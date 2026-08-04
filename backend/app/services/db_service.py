from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlparse

from app.config import Settings
from app.services.tool_router import ToolRouter
from app.services.vision_service import VisionAnnotator

logger = logging.getLogger(__name__)


class InspectionDBClient:
    """SQLite client for the MTR inspection object database (new multi-inspection schema).

    Schema: ``categories``, ``inspections``, ``images``, ``objects``, ``detections``
    (plus ``anomaly_types`` / ``abnormal_detections`` / ``abnormalities``, added by the
    grounding writer later). An object has a category, centroid and 3D bbox, and appears
    in one or more per-frame ``detections`` linked to ``images`` (which carry the camera
    pose and timestamp). Detection counts and first/last-seen timestamps are derived via
    those joins. Tools accept an optional ``inspection_id`` so the assistant can reason
    across inspections.
    """

    # Category aliases map colloquial terms to the category names stored in `categories`.
    _CATEGORY_ALIASES: dict[str, str] = {
        "advertisement board": "Advertisement Board",
        "ad board": "Advertisement Board",
        "adboard": "Advertisement Board",
        "poster": "Advertisement Board",
        "billboard": "Advertisement Board",
        "exit sign": "Exit Sign",
        "exit": "Exit Sign",
        "light": "Lights",
        "lights": "Lights",
        "map": "Map",
        "tv": "TV",
        "television": "TV",
        "ticket gate": "Ticket Gate",
        "gate": "Ticket Gate",
    }

    @property
    def last_tool_calls(self) -> list[dict[str, Any]]:
        return self._last_tool_calls

    @property
    def last_tool_results(self) -> list[dict[str, Any]]:
        return self._last_tool_results

    @property
    def last_highlight_status(self) -> str | None:
        """Status string from the final-pass Rerun highlight (None if nothing was pushed)."""
        return self._last_highlight_status

    @property
    def last_highlight_args(self) -> dict[str, Any] | None:
        """Normalized args of the last Rerun highlight push (for the debug panel)."""
        return self._last_highlight_args

    @property
    def highlight_history(self) -> list[dict[str, Any]]:
        """All Rerun highlight commands issued during this session, oldest first."""
        return list(self._highlight_history)

    @property
    def rerun_job_stats(self) -> dict[str, Any] | None:
        """Background Rerun worker counters (for debugging viewer staleness)."""
        if self.rerun_visualizer is None:
            return None
        return self.rerun_visualizer.job_stats()

    def _record_highlight(self, args: dict[str, Any], status: str | None) -> None:
        """Append a highlight call to the session history for debugging."""
        self._highlight_history.append(
            {
                "args": dict(args),
                "status": status,
                "query": self._last_query,
            }
        )

    def _record_tool_calls(self, tool_calls: list[tuple[str, dict[str, Any]]]) -> None:
        self._last_tool_calls = [
            {"name": name, "args": args} for name, args in tool_calls
        ]

    def _record_tool_results(self, tool_results: list[dict[str, Any]]) -> None:
        self._last_tool_results = tool_results

    @classmethod
    def _canonical_category(cls, name: str) -> str:
        """Normalize a category name from the LLM to the exact DB category string."""
        key = name.strip().lower()
        if key in cls._CATEGORY_ALIASES:
            return cls._CATEGORY_ALIASES[key]
        for alias, canonical in cls._CATEGORY_ALIASES.items():
            if canonical.lower() == key:
                return canonical
        return name.strip()

    @staticmethod
    def _resolve_limit(value: Any, default: int | None = None) -> int | None:
        """Normalize a tool limit argument. 'all' means no cap."""
        if value is None:
            return default
        if isinstance(value, str) and value.strip().lower() == "all":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _format_xyz(x: Any, y: Any, z: Any, prefix: str = "") -> str:
        """Format x,y,z as '[x, y, z]' or return '<prefix>unknown' if any are None.

        Square brackets are used instead of parentheses so the answering LLM does
        not render the coordinates as LaTeX math (e.g. $(x, y, z)$).
        """
        if x is None or y is None or z is None:
            return f"{prefix}unknown"
        return f"{prefix}[{float(x):.2f}, {float(y):.2f}, {float(z):.2f}]"

    def __init__(
        self,
        db_path: str | Path,
        router: ToolRouter | None = None,
        settings: Settings | None = None,
        vision_annotator: VisionAnnotator | None = None,
        rerun_visualizer: Any | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.router = router
        self.settings = settings or (router.settings if router else None)
        self.vision_annotator = vision_annotator
        self.rerun_visualizer = rerun_visualizer
        self._conn: sqlite3.Connection | None = None
        self._last_tool_calls: list[dict[str, Any]] = []
        self._last_tool_results: list[dict[str, Any]] = []
        self._last_query: str = ""
        self._last_chat_history: list[tuple[str, str]] = []
        self._last_highlight_status: str | None = None
        # Normalized args of the last Rerun highlight (decider, explicit tool, or
        # merged fallback) so the debug panel can show what was pushed to the viewer.
        self._last_highlight_args: dict[str, Any] | None = None
        # Session history of every Rerun highlight command, for debugging why the
        # viewer did or did not update.
        self._highlight_history: list[dict[str, Any]] = []
        # Cache for the full anomaly trio (report + locations + details) so generic
        # anomaly follow-ups reuse the previous turn instead of re-querying.
        self._last_anomaly_trio_context: str | None = None
        self._last_anomaly_trio_calls: list[tuple[str, dict[str, Any]]] = []
        self._last_anomaly_trio_results: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Timestamp / image helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_timestamp(ns: int | None) -> str:
        if ns is None:
            return "unknown"
        try:
            dt = datetime.fromtimestamp(ns / 1e9)
            base = dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            ampm = dt.strftime("%I:%M %p").lstrip("0").replace(" AM", "am").replace(" PM", "pm")
            return f"{base} ({ampm})"
        except (OSError, OverflowError, ValueError):
            return str(ns)

    @staticmethod
    def _format_duration(seconds: float) -> str:
        if seconds < 60:
            return f"{seconds:.1f} seconds"
        if seconds < 3600:
            return f"{seconds / 60:.1f} minutes"
        return f"{seconds / 3600:.2f} hours"

    @staticmethod
    def _image_url(image_path: str | None) -> str | None:
        """Map a filename (or path) to the frontend image URL `/inspection/images/<name>`."""
        if not image_path:
            return None
        name = Path(image_path).name
        if not name:
            return None
        return f"/inspection/images/{name}"

    def _image_objects_info(self, filename: str) -> str:
        """Compact 'Object <id> (<category>)' summary of every object seen in one frame.

        Used to annotate image links with the object IDs they contain, so the LLM can
        answer 'which object ID' questions even when the user only asked for images.
        """
        if not filename:
            return ""
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT DISTINCT o.id, c.name AS category
            FROM detections d
            JOIN images i ON i.id = d.image_id
            JOIN objects o ON o.id = d.object_id
            JOIN categories c ON c.id = o.category_id
            WHERE i.filename = ? AND o.id IS NOT NULL
            ORDER BY o.id ASC
            """,
            (filename,),
        ).fetchall()
        if not rows:
            return ""
        parts = [f"Object {row['id']} ({row['category'] or 'unknown'})" for row in rows]
        return ", ".join(parts)

    _IMAGE_URL_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
    # Match canonical image URL prefixes served by the backend.
    _PLAIN_IMAGE_RE = re.compile(
        r"((?:/(?:inspection|annotated)/images/)[^\s\)\"]+)"
    )

    @staticmethod
    def _extract_image_urls(text: str) -> list[str]:
        """Return image URLs found in a text turn, in order of appearance."""
        urls: list[str] = []
        for match in InspectionDBClient._IMAGE_URL_RE.finditer(text):
            urls.append(match.group(1))
        for match in InspectionDBClient._PLAIN_IMAGE_RE.finditer(text):
            if match.group(1) not in urls:
                urls.append(match.group(1))
        return urls

    @classmethod
    def _image_urls_from_history(cls, chat_history: Sequence[tuple[str, str]] | None) -> list[str]:
        """Collect image URLs from assistant turns in chronological order."""
        if not chat_history:
            return []
        urls: list[str] = []
        for _, assistant_text in chat_history:
            if not assistant_text:
                continue
            for url in cls._extract_image_urls(assistant_text):
                if url not in urls:
                    urls.append(url)
        return urls

    @classmethod
    def _resolve_image_reference(
        cls,
        query: str,
        chat_history: Sequence[tuple[str, str]] | None,
    ) -> str | None:
        """Resolve phrases like 'first image', 'last image', 'that image' to a URL."""
        urls = cls._image_urls_from_history(chat_history)
        if not urls:
            return None

        q = query.lower()

        url_match = cls._PLAIN_IMAGE_RE.search(q)
        if url_match:
            return url_match.group(1)

        ordinal_match = re.search(r"\b(first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th|last)\s+image\b", q)
        if ordinal_match:
            word = ordinal_match.group(1)
            if word in ("first", "1st"):
                return urls[0]
            if word in ("second", "2nd"):
                return urls[1] if len(urls) > 1 else urls[-1]
            if word in ("third", "3rd"):
                return urls[2] if len(urls) > 2 else urls[-1]
            if word in ("fourth", "4th"):
                return urls[3] if len(urls) > 3 else urls[-1]
            if word in ("fifth", "5th"):
                return urls[4] if len(urls) > 4 else urls[-1]
            if word == "last":
                return urls[-1]

        numeric_match = re.search(r"\bimage\s+(\d+)\b", q)
        if numeric_match:
            idx = int(numeric_match.group(1)) - 1
            if 0 <= idx < len(urls):
                return urls[idx]
            return urls[-1]

        if re.search(r"\b(that|this|the|it)\s+image\b|\bthat image\b|\bthis image\b|\bthe image\b|\bdraw (?:on|over) it\b|\bon it\b", q):
            return urls[-1]

        return None

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Reusable query helpers
    # ------------------------------------------------------------------

    def _category_id(self, name: str) -> int | None:
        conn = self._connect()
        row = conn.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()
        return int(row["id"]) if row else None

    def _anomaly_tables_exist(self) -> bool:
        """True if the writer has added the anomaly tables to this database."""
        conn = self._connect()
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('anomaly_types','abnormal_detections','abnormalities')"
        ).fetchall()
        return len(rows) == 3

    def _object_rows(
        self,
        where_sql: str = "",
        params: Sequence[Any] = (),
        *,
        order: str = "o.id",
        limit: int | None = None,
        inspection_id: int | None = None,
    ) -> list[sqlite3.Row]:
        """Return object rows with derived detection_count / first_seen_ns / last_seen_ns.

        ``where_sql`` is appended after ``WHERE`` (joined with AND to the inspection
        scope clause when present). ``params`` bind to ``where_sql`` only; the
        inspection_id parameter is bound last.
        """
        conn = self._connect()
        clauses: list[str] = []
        if where_sql:
            clauses.append(where_sql)
        if inspection_id is not None:
            clauses.append(
                "o.id IN (SELECT d.object_id FROM detections d "
                "JOIN images i ON i.id=d.image_id WHERE i.inspection_id=?)"
            )
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        order_clause = f" ORDER BY {order}" if order else ""
        limit_clause = f" LIMIT {int(limit)}" if limit else ""
        sql = (
            "SELECT o.id, c.name AS category, "
            "o.centroid_x, o.centroid_y, o.centroid_z, "
            "o.min_x, o.min_y, o.min_z, o.max_x, o.max_y, o.max_z, "
            "o.is_gt, o.created_at, "
            "(SELECT COUNT(*) FROM detections d WHERE d.object_id=o.id) AS detection_count, "
            "(SELECT MIN(i.timestamp_ns) FROM detections d JOIN images i ON i.id=d.image_id "
            "WHERE d.object_id=o.id) AS first_seen_ns, "
            "(SELECT MAX(i.timestamp_ns) FROM detections d JOIN images i ON i.id=d.image_id "
            "WHERE d.object_id=o.id) AS last_seen_ns, "
            # Each object belongs to exactly one inspection (verified on the current DB),
            # so MIN is just a safe scalar pick of that single inspection id.
            "(SELECT MIN(i.inspection_id) FROM detections d JOIN images i ON i.id=d.image_id "
            "WHERE d.object_id=o.id) AS inspection_id "
            "FROM objects o JOIN categories c ON c.id=o.category_id"
            + where + order_clause + limit_clause
        )
        all_params = list(params)
        if inspection_id is not None:
            all_params.append(inspection_id)
        return conn.execute(sql, tuple(all_params)).fetchall()

    # ------------------------------------------------------------------
    # Structured queries
    # ------------------------------------------------------------------

    def get_summary(self, inspection_id: int | None = None, top_n: int = 5) -> dict[str, Any]:
        """Overall counts, category breakdown, and a few notable objects."""
        conn = self._connect()
        if inspection_id is None:
            total_objects = conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
        else:
            total_objects = conn.execute(
                "SELECT COUNT(DISTINCT o.id) FROM objects o "
                "JOIN detections d ON d.object_id=o.id JOIN images i ON i.id=d.image_id "
                "WHERE i.inspection_id=?",
                (inspection_id,),
            ).fetchone()[0]

        categories = self._category_counts(inspection_id)
        objects = self._object_rows(order="detection_count DESC", limit=top_n, inspection_id=inspection_id)
        return {
            "total_objects": total_objects,
            "categories": categories,
            "objects": [dict(row) for row in objects],
            "inspection_id": inspection_id,
        }

    def _category_counts(self, inspection_id: int | None = None) -> list[dict[str, Any]]:
        conn = self._connect()
        if inspection_id is None:
            rows = conn.execute(
                "SELECT c.name AS category, COUNT(*) AS count "
                "FROM objects o JOIN categories c ON c.id=o.category_id "
                "GROUP BY c.name ORDER BY count DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT c.name AS category, COUNT(DISTINCT o.id) AS count "
                "FROM objects o JOIN categories c ON c.id=o.category_id "
                "WHERE o.id IN (SELECT d.object_id FROM detections d JOIN images i ON i.id=d.image_id WHERE i.inspection_id=?) "
                "GROUP BY c.name ORDER BY count DESC",
                (inspection_id,),
            ).fetchall()
        return [{"category": row["category"], "count": row["count"]} for row in rows]

    def get_categories(self) -> list[str]:
        """Return the distinct category names present in the categories table."""
        conn = self._connect()
        rows = conn.execute("SELECT name FROM categories ORDER BY name").fetchall()
        return [row["name"] for row in rows]

    def get_inspections(self) -> list[dict[str, Any]]:
        """List inspections with per-inspection object and detection counts."""
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT ins.id, ins.started_at, ins.is_gt,
                   (SELECT COUNT(DISTINCT d.object_id) FROM detections d
                    JOIN images i ON i.id=d.image_id WHERE i.inspection_id=ins.id) AS object_count,
                   (SELECT COUNT(*) FROM detections d
                    JOIN images i ON i.id=d.image_id WHERE i.inspection_id=ins.id) AS detection_count
            FROM inspections ins
            ORDER BY ins.id
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def _inspection_meta(self) -> dict[int, bool]:
        """Map inspection id -> is_gt flag (empty dict if no inspections table rows)."""
        try:
            return {ins["id"]: bool(ins["is_gt"]) for ins in self.get_inspections()}
        except Exception:  # noqa: BLE001
            return {}

    def _inspection_tag(self, inspection_id: Any, meta: dict[int, bool]) -> str:
        """Short label like 'inspection 2' or 'inspection 1 (ground truth)'."""
        if inspection_id is None:
            return "unknown inspection"
        tag = f"inspection {inspection_id}"
        if meta.get(int(inspection_id)):
            tag += " (ground truth)"
        return tag

    def _category_counts_by_inspection(self, category: str) -> list[dict[str, Any]]:
        """Distinct object counts for one category, broken down per inspection."""
        conn = self._connect()
        rows = conn.execute(
            "SELECT i.inspection_id, COUNT(DISTINCT o.id) AS count "
            "FROM objects o JOIN categories c ON c.id=o.category_id "
            "JOIN detections d ON d.object_id=o.id JOIN images i ON i.id=d.image_id "
            "WHERE c.name=? GROUP BY i.inspection_id ORDER BY i.inspection_id",
            (category,),
        ).fetchall()
        return [{"inspection_id": row["inspection_id"], "count": row["count"]} for row in rows]

    def query_database(self, sql_query: str, limit: int | str | None = 100) -> dict[str, Any]:
        """Execute a read-only SELECT query and return the results."""
        return self.run_sql_query(sql_query, limit)

    def get_objects_by_category(self, category: str, limit: int | str | None = None, inspection_id: int | None = None) -> list[dict[str, Any]]:
        rows = self._object_rows(
            "c.name = ?", (category,), order="detection_count DESC", limit=self._resolve_limit(limit), inspection_id=inspection_id
        )
        return [dict(row) for row in rows]

    def get_category_objects_with_coordinates(self, category: str, inspection_id: int | None = None) -> list[dict[str, Any]]:
        rows = self._object_rows("c.name = ?", (category,), order="first_seen_ns", inspection_id=inspection_id)
        return [dict(row) for row in rows]

    def get_category_objects_with_images(
        self, category: str, limit: int | str | None = None, inspection_id: int | None = None
    ) -> list[dict[str, Any]]:
        """Return objects in a category with centroid and one sample image filename each."""
        rows = self._object_rows(
            "c.name = ?", (category,), order="first_seen_ns", limit=self._resolve_limit(limit), inspection_id=inspection_id
        )
        conn = self._connect()
        out: list[dict[str, Any]] = []
        for row in rows:
            sample = conn.execute(
                "SELECT i.filename FROM detections d JOIN images i ON i.id=d.image_id "
                "WHERE d.object_id=? AND i.filename IS NOT NULL ORDER BY i.timestamp_ns LIMIT 1",
                (row["id"],),
            ).fetchone()
            out.append({**dict(row), "sample_image_path": sample["filename"] if sample else None})
        return out

    def get_category_proximity(
        self,
        target_category: str,
        other_categories: list[str],
        radius_m: float = 2.0,
        inspection_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """For each object in *target_category*, count how many objects from
        *other_categories* have centroids within *radius_m* meters."""
        targets = self._object_rows(
            "c.name = ?", (target_category,), inspection_id=inspection_id
        )
        if not targets or not other_categories:
            return []
        placeholders = ",".join("?" for _ in other_categories)
        others = self._object_rows(
            f"c.name IN ({placeholders})", tuple(other_categories), inspection_id=inspection_id
        )

        results = []
        for target in targets:
            tx, ty, tz = target["centroid_x"], target["centroid_y"], target["centroid_z"]
            if tx is None or ty is None or tz is None:
                continue
            nearby: dict[str, int] = {}
            for row in others:
                if row["id"] == target["id"]:
                    continue
                ox, oy, oz = row["centroid_x"], row["centroid_y"], row["centroid_z"]
                if ox is None or oy is None or oz is None:
                    continue
                dx, dy, dz = ox - tx, oy - ty, oz - tz
                dist = (dx * dx + dy * dy + dz * dz) ** 0.5
                if dist <= radius_m:
                    cat = row["category"]
                    nearby[cat] = nearby.get(cat, 0) + 1
            results.append(
                {
                    "object_id": target["id"],
                    "centroid_x": tx,
                    "centroid_y": ty,
                    "centroid_z": tz,
                    "nearby": nearby,
                }
            )
        return results

    def get_category_proximity_with_images(
        self,
        target_category: str,
        other_categories: list[str],
        radius_m: float = 2.0,
        limit: int | str | None = None,
        nearby_limit: int | str | None = None,
        inspection_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """For each target-category object, return nearby objects (with sample image).

        Returns up to *limit* target objects; for each target, up to *nearby_limit*
        nearest objects from *other_categories* within *radius_m*, including their
        distance and a sample frame URL.
        """
        limit = self._resolve_limit(limit)
        nearby_limit = self._resolve_limit(nearby_limit)
        targets = self._object_rows(
            "c.name = ?", (target_category,), order="first_seen_ns", limit=limit, inspection_id=inspection_id
        )
        if not targets or not other_categories:
            return []
        placeholders = ",".join("?" for _ in other_categories)
        others = self._object_rows(
            f"c.name IN ({placeholders})", tuple(other_categories), inspection_id=inspection_id
        )

        conn = self._connect()
        results: list[dict[str, Any]] = []
        for target in targets:
            tx, ty, tz = target["centroid_x"], target["centroid_y"], target["centroid_z"]
            if tx is None or ty is None or tz is None:
                continue
            nearby: list[dict[str, Any]] = []
            for row in others:
                if row["id"] == target["id"]:
                    continue
                ox, oy, oz = row["centroid_x"], row["centroid_y"], row["centroid_z"]
                if ox is None or oy is None or oz is None:
                    continue
                dx, dy, dz = ox - tx, oy - ty, oz - tz
                dist = (dx * dx + dy * dy + dz * dz) ** 0.5
                if dist <= radius_m:
                    sample = conn.execute(
                        "SELECT i.filename FROM detections d JOIN images i ON i.id=d.image_id "
                        "WHERE d.object_id=? AND i.filename IS NOT NULL ORDER BY i.timestamp_ns LIMIT 1",
                        (row["id"],),
                    ).fetchone()
                    nearby.append(
                        {
                            "object_id": row["id"],
                            "category": row["category"],
                            "distance_m": round(dist, 3),
                            "centroid_x": ox,
                            "centroid_y": oy,
                            "centroid_z": oz,
                            "sample_image_path": sample["filename"] if sample else None,
                        }
                    )
            nearby.sort(key=lambda r: r["distance_m"])
            results.append(
                {
                    "object_id": target["id"],
                    "centroid_x": tx,
                    "centroid_y": ty,
                    "centroid_z": tz,
                    "nearby": nearby[:nearby_limit] if nearby_limit is not None else nearby,
                }
            )
        return results

    def get_objects_proximity_with_images(
        self,
        object_ids: list[int],
        target_category: str,
        radius_m: float = 2.0,
        limit: int | str | None = None,
        nearby_limit: int | str | None = None,
        inspection_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """For each object in *object_ids*, return nearby objects of *target_category*.

        This is the object-id counterpart of ``get_category_proximity_with_images``:
        the caller supplies specific source object IDs (e.g. objects 9 and 11) and
        a target category (e.g. Lights), and the method returns the Lights that are
        within *radius_m* of each source object, with distances, coordinates, and
        sample frames.
        """
        limit = self._resolve_limit(limit)
        nearby_limit = self._resolve_limit(nearby_limit)
        if not object_ids:
            return []
        placeholders = ",".join("?" for _ in object_ids)
        conn = self._connect()
        sources = conn.execute(
            f"""
            SELECT o.id, c.name AS category,
                   o.centroid_x, o.centroid_y, o.centroid_z
            FROM objects o JOIN categories c ON c.id=o.category_id
            WHERE o.id IN ({placeholders})
            ORDER BY o.id
            """,
            tuple(object_ids),
        ).fetchall()
        sources = [dict(row) for row in sources]
        if not sources:
            return []
        if limit is not None:
            sources = sources[:limit]

        targets = self._object_rows(
            "c.name = ?", (target_category,), inspection_id=inspection_id
        )
        if not targets:
            return []

        results: list[dict[str, Any]] = []
        for source in sources:
            sx, sy, sz = source["centroid_x"], source["centroid_y"], source["centroid_z"]
            if sx is None or sy is None or sz is None:
                continue
            nearby: list[dict[str, Any]] = []
            for row in targets:
                if row["id"] == source["id"]:
                    continue
                tx, ty, tz = row["centroid_x"], row["centroid_y"], row["centroid_z"]
                if tx is None or ty is None or tz is None:
                    continue
                dx, dy, dz = tx - sx, ty - sy, tz - sz
                dist = (dx * dx + dy * dy + dz * dz) ** 0.5
                if dist <= radius_m:
                    sample = conn.execute(
                        "SELECT i.filename FROM detections d JOIN images i ON i.id=d.image_id "
                        "WHERE d.object_id=? AND i.filename IS NOT NULL ORDER BY i.timestamp_ns LIMIT 1",
                        (row["id"],),
                    ).fetchone()
                    nearby.append(
                        {
                            "object_id": row["id"],
                            "category": row["category"],
                            "distance_m": round(dist, 3),
                            "centroid_x": tx,
                            "centroid_y": ty,
                            "centroid_z": tz,
                            "sample_image_path": sample["filename"] if sample else None,
                        }
                    )
            nearby.sort(key=lambda r: r["distance_m"])
            results.append(
                {
                    "source_object_id": source["id"],
                    "source_category": source.get("category"),
                    "centroid_x": sx,
                    "centroid_y": sy,
                    "centroid_z": sz,
                    "nearby": nearby[:nearby_limit] if nearby_limit is not None else nearby,
                }
            )
        return results

    def get_object_by_id(self, object_id: int) -> dict[str, Any] | None:
        conn = self._connect()
        row = conn.execute(
            """
            SELECT o.id, c.name AS category,
                   o.centroid_x, o.centroid_y, o.centroid_z,
                   o.min_x, o.min_y, o.min_z, o.max_x, o.max_y, o.max_z,
                   o.is_gt, o.created_at,
                   (SELECT COUNT(*) FROM detections d WHERE d.object_id=o.id) AS detection_count,
                   (SELECT MIN(i.timestamp_ns) FROM detections d JOIN images i ON i.id=d.image_id
                    WHERE d.object_id=o.id) AS first_seen_ns,
                   (SELECT MAX(i.timestamp_ns) FROM detections d JOIN images i ON i.id=d.image_id
                    WHERE d.object_id=o.id) AS last_seen_ns,
                   (SELECT MIN(i.inspection_id) FROM detections d JOIN images i ON i.id=d.image_id
                    WHERE d.object_id=o.id) AS inspection_id
            FROM objects o JOIN categories c ON c.id=o.category_id
            WHERE o.id = ?
            """,
            (object_id,),
        ).fetchone()
        if row is None:
            return None
        obj = dict(row)
        obj["detections"] = conn.execute(
            """
            SELECT d.id, i.timestamp_ns, i.filename, i.inspection_id,
                   d.centroid_x, d.centroid_y, d.centroid_z,
                   d.min_x, d.min_y, d.min_z, d.max_x, d.max_y, d.max_z
            FROM detections d JOIN images i ON i.id=d.image_id
            WHERE d.object_id = ?
            ORDER BY i.timestamp_ns
            """,
            (object_id,),
        ).fetchall()
        return obj

    def get_top_objects(self, n: int | str | None = 5, inspection_id: int | None = None) -> list[dict[str, Any]]:
        rows = self._object_rows(order="detection_count DESC", limit=self._resolve_limit(n, default=5), inspection_id=inspection_id)
        return [dict(row) for row in rows]

    def get_recent_objects(self, limit: int | str | None = 5, inspection_id: int | None = None) -> list[dict[str, Any]]:
        rows = self._object_rows(order="last_seen_ns DESC", limit=self._resolve_limit(limit, default=5), inspection_id=inspection_id)
        return [dict(row) for row in rows]

    def get_object_timeline(self, object_id: int) -> list[dict[str, Any]]:
        """Return every detection for an object, ordered by timestamp."""
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT i.timestamp_ns, i.filename, d.centroid_x, d.centroid_y, d.centroid_z
            FROM detections d JOIN images i ON i.id=d.image_id
            WHERE d.object_id = ?
            ORDER BY i.timestamp_ns
            """,
            (object_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_object_image_paths(self, object_id: int) -> list[str]:
        """Return distinct image URLs for an object."""
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT DISTINCT i.filename
            FROM detections d JOIN images i ON i.id=d.image_id
            WHERE d.object_id = ? AND i.filename IS NOT NULL
            ORDER BY i.timestamp_ns
            """,
            (object_id,),
        ).fetchall()
        return [self._image_url(row["filename"]) for row in rows if row["filename"]]

    def get_objects_in_image(self, filename: str) -> list[dict[str, Any]]:
        """Return every object detected in a single image frame."""
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT DISTINCT o.id AS object_id, c.name AS category,
                   o.centroid_x, o.centroid_y, o.centroid_z,
                   d.centroid_x AS det_centroid_x,
                   d.centroid_y AS det_centroid_y,
                   d.centroid_z AS det_centroid_z
            FROM detections d
            JOIN images i ON i.id = d.image_id
            JOIN objects o ON o.id = d.object_id
            JOIN categories c ON c.id = o.category_id
            WHERE i.filename = ?
            ORDER BY o.id
            """,
            (Path(filename).name,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_category_timeline(self, category: str, inspection_id: int | None = None) -> list[dict[str, Any]]:
        """Return first/last seen timestamps for every object in a category."""
        rows = self._object_rows("c.name = ?", (category,), order="first_seen_ns", inspection_id=inspection_id)
        return [dict(row) for row in rows]

    def get_inspection_timeline(self, inspection_id: int | None = None) -> list[dict[str, Any]]:
        """Return every object ordered by first-seen timestamp."""
        rows = self._object_rows(order="first_seen_ns", inspection_id=inspection_id)
        return [dict(row) for row in rows]

    def get_category_windows(self, categories: list[str], inspection_id: int | None = None) -> list[dict[str, Any]]:
        """Return first/last detection windows for one or more categories."""
        if not categories:
            return []
        conn = self._connect()
        placeholders = ",".join("?" for _ in categories)
        scope = ""
        params: list[Any] = list(categories)
        if inspection_id is not None:
            scope = " AND i.inspection_id=?"
            params.append(inspection_id)
        rows = conn.execute(
            f"""
            SELECT c.name AS category,
                   MIN(i.timestamp_ns) AS first_seen_ns,
                   MAX(i.timestamp_ns) AS last_seen_ns,
                   COUNT(DISTINCT o.id) AS object_count
            FROM detections d
            JOIN images i ON i.id=d.image_id
            JOIN objects o ON o.id=d.object_id
            JOIN categories c ON c.id=o.category_id
            WHERE c.name IN ({placeholders}){scope}
            GROUP BY c.name
            ORDER BY first_seen_ns
            """,
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_temporal_clusters(self, window_ms: int = 500, top_n: int = 20, inspection_id: int | None = None) -> list[dict[str, Any]]:
        """Group detections into time-window clusters and count categories in each.

        A cluster is a consecutive run of detection timestamps within *window_ms* of
        each other, revealing which kinds of objects were seen together at a moment.
        """
        conn = self._connect()
        scope = " WHERE i.inspection_id=?" if inspection_id is not None else ""
        params: tuple[Any, ...] = (inspection_id,) if inspection_id is not None else ()
        rows = conn.execute(
            f"""
            SELECT i.timestamp_ns, c.name AS category
            FROM detections d
            JOIN images i ON i.id=d.image_id
            JOIN objects o ON o.id=d.object_id
            JOIN categories c ON c.id=o.category_id
            WHERE i.timestamp_ns IS NOT NULL{scope}
            ORDER BY i.timestamp_ns
            """,
            params,
        ).fetchall()

        if not rows:
            return []

        window_ns = window_ms * 1_000_000
        clusters: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for row in rows:
            ts = row["timestamp_ns"]
            category = row["category"]
            if current is None or ts - current["end_ns"] > window_ns:
                if current is not None:
                    clusters.append(current)
                current = {"start_ns": ts, "end_ns": ts, "categories": {}, "detection_count": 0}
            current["end_ns"] = ts
            current["categories"][category] = current["categories"].get(category, 0) + 1
            current["detection_count"] += 1
        if current is not None:
            clusters.append(current)

        clusters.sort(key=lambda c: c["detection_count"], reverse=True)
        return clusters[:top_n]

    # ------------------------------------------------------------------
    # Additional spatial / temporal helpers
    # ------------------------------------------------------------------

    def _get_inspection_base_date(self) -> datetime.date:
        """Return the date of the earliest image, or today if none."""
        conn = self._connect()
        row = conn.execute("SELECT MIN(timestamp_ns) FROM images").fetchone()
        if row and row[0]:
            return datetime.fromtimestamp(row[0] / 1e9).date()
        return datetime.now().date()

    def _get_inspection_time_range_ns(self) -> tuple[int, int]:
        """Return (min_timestamp_ns, max_timestamp_ns) from images."""
        conn = self._connect()
        row = conn.execute("SELECT MIN(timestamp_ns), MAX(timestamp_ns) FROM images").fetchone()
        if row and row[0] and row[1]:
            return int(row[0]), int(row[1])
        return 0, 0

    def _parse_time_string(self, value: str | int | float) -> int | None:
        """Convert a time string or integer into nanoseconds since epoch.

        Supports integer/float nanoseconds, ISO datetime strings, and clock-only
        strings (e.g. '16:51:45', '4:51 PM') interpreted on the inspection date.
        """
        if isinstance(value, (int, float)):
            return int(value)
        s = str(value).strip()
        try:
            return int(s)
        except ValueError:
            pass
        for fmt in (
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
        ):
            try:
                dt = datetime.strptime(s, fmt)
                return int(dt.timestamp() * 1e9)
            except ValueError:
                continue

        base = self._get_inspection_base_date()
        time_only_formats = ("%H:%M:%S.%f", "%H:%M:%S", "%H:%M", "%I:%M %p", "%I:%M:%S %p")
        min_ns, max_ns = self._get_inspection_time_range_ns()
        for fmt in time_only_formats:
            try:
                t = datetime.strptime(s, fmt).time()
                dt = datetime.combine(base, t)
                ns = int(dt.timestamp() * 1e9)
                if min_ns and max_ns and not (min_ns <= ns <= max_ns):
                    shifted = ns + int(12 * 3600 * 1e9)
                    if min_ns <= shifted <= max_ns:
                        return shifted
                    shifted = ns - int(12 * 3600 * 1e9)
                    if min_ns <= shifted <= max_ns:
                        return shifted
                return ns
            except ValueError:
                continue

        logger.warning("Could not parse time string: %r", s)
        return None

    def get_detection_counts_by_category(self, inspection_id: int | None = None) -> list[dict[str, Any]]:
        """Per-frame detection counts by category."""
        conn = self._connect()
        if inspection_id is None:
            rows = conn.execute(
                "SELECT c.name AS category, COUNT(*) AS count "
                "FROM detections d JOIN objects o ON o.id=d.object_id "
                "JOIN categories c ON c.id=o.category_id "
                "GROUP BY c.name ORDER BY count DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT c.name AS category, COUNT(*) AS count "
                "FROM detections d JOIN images i ON i.id=d.image_id "
                "JOIN objects o ON o.id=d.object_id JOIN categories c ON c.id=o.category_id "
                "WHERE i.inspection_id=? GROUP BY c.name ORDER BY count DESC",
                (inspection_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_objects_in_time_range(
        self, start_time: str | int | float, end_time: str | int | float, limit: int | str | None = None, inspection_id: int | None = None
    ) -> list[dict[str, Any]]:
        """Objects whose detection span overlaps [start, end]."""
        start_ns = self._parse_time_string(start_time)
        end_ns = self._parse_time_string(end_time)
        if start_ns is None or end_ns is None:
            return []
        where = (
            "(SELECT MIN(i.timestamp_ns) FROM detections d JOIN images i ON i.id=d.image_id "
            "WHERE d.object_id=o.id) <= ? AND "
            "(SELECT MAX(i.timestamp_ns) FROM detections d JOIN images i ON i.id=d.image_id "
            "WHERE d.object_id=o.id) >= ?"
        )
        rows = self._object_rows(
            where, (end_ns, start_ns), order="first_seen_ns", limit=self._resolve_limit(limit), inspection_id=inspection_id
        )
        return [dict(row) for row in rows]

    def get_detections_in_time_range(
        self, start_time: str | int | float, end_time: str | int | float, limit: int | str | None = None, inspection_id: int | None = None
    ) -> list[dict[str, Any]]:
        """Per-frame detections captured within [start, end]."""
        start_ns = self._parse_time_string(start_time)
        end_ns = self._parse_time_string(end_time)
        if start_ns is None or end_ns is None:
            return []
        limit = self._resolve_limit(limit)
        conn = self._connect()
        scope = " AND i.inspection_id=?" if inspection_id is not None else ""
        params: list[Any] = [start_ns, end_ns]
        if inspection_id is not None:
            params.append(inspection_id)
        limit_clause = " LIMIT ?" if limit is not None else ""
        if limit is not None:
            params.append(limit)
        rows = conn.execute(
            f"""
            SELECT i.timestamp_ns, d.object_id, c.name AS category, i.filename,
                   d.centroid_x, d.centroid_y, d.centroid_z
            FROM detections d
            JOIN images i ON i.id=d.image_id
            JOIN objects o ON o.id=d.object_id
            JOIN categories c ON c.id=o.category_id
            WHERE i.timestamp_ns >= ? AND i.timestamp_ns <= ?{scope}
            ORDER BY i.timestamp_ns{limit_clause}
            """,
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_objects_near_position(
        self,
        x: float,
        y: float,
        z: float,
        radius_m: float = 2.0,
        category: str | None = None,
        inspection_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Objects whose centroid is within radius_m of (x, y, z)."""
        where = "o.centroid_x IS NOT NULL"
        params: list[Any] = []
        if category:
            where += " AND c.name = ?"
            params.append(category)
        rows = self._object_rows(where, tuple(params), inspection_id=inspection_id)
        results = []
        for row in rows:
            dx = row["centroid_x"] - x
            dy = row["centroid_y"] - y
            dz = row["centroid_z"] - z
            dist = (dx * dx + dy * dy + dz * dz) ** 0.5
            if dist <= radius_m:
                results.append({**dict(row), "distance_m": round(dist, 3)})
        results.sort(key=lambda r: r["distance_m"])
        return results

    def get_category_sample_images(self, category: str, limit: int | str | None = None, inspection_id: int | None = None) -> list[str]:
        """Distinct image URLs for a category, sampled randomly."""
        limit = self._resolve_limit(limit)
        conn = self._connect()
        scope = " AND i.inspection_id=?" if inspection_id is not None else ""
        params: list[Any] = [category]
        if inspection_id is not None:
            params.append(inspection_id)
        limit_clause = " LIMIT ?" if limit is not None else ""
        if limit is not None:
            params.append(limit)
        rows = conn.execute(
            f"""
            SELECT DISTINCT i.filename
            FROM detections d
            JOIN images i ON i.id=d.image_id
            JOIN objects o ON o.id=d.object_id
            JOIN categories c ON c.id=o.category_id
            WHERE c.name = ? AND i.filename IS NOT NULL{scope}
            ORDER BY RANDOM(){limit_clause}
            """,
            tuple(params),
        ).fetchall()
        return [self._image_url(row["filename"]) for row in rows if row["filename"]]

    def get_inspection_poses(self, limit: int | str | None = None, inspection_id: int | None = None) -> list[dict[str, Any]]:
        """Camera/robot poses (one per image), now stored on the images table."""
        limit = self._resolve_limit(limit)
        conn = self._connect()
        scope = " WHERE inspection_id=?" if inspection_id is not None else ""
        params: list[Any] = [inspection_id] if inspection_id is not None else []
        limit_clause = " LIMIT ?" if limit is not None else ""
        if limit is not None:
            params.append(limit)
        rows = conn.execute(
            f"""
            SELECT id, inspection_id, filename,
                   tf_translation_x, tf_translation_y, tf_translation_z,
                   tf_rotation_x, tf_rotation_y, tf_rotation_z, tf_rotation_w
            FROM images{scope}{limit_clause}
            """,
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_object_distance(self, object_id_a: int, object_id_b: int) -> dict[str, Any] | None:
        """Distance between the centroids of two objects."""
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT o.id, c.name AS category, o.centroid_x, o.centroid_y, o.centroid_z
            FROM objects o JOIN categories c ON c.id=o.category_id
            WHERE o.id IN (?, ?)
            """,
            (object_id_a, object_id_b),
        ).fetchall()
        if len(rows) != 2:
            return None
        a, b = rows[0], rows[1]
        dx = a["centroid_x"] - b["centroid_x"]
        dy = a["centroid_y"] - b["centroid_y"]
        dz = a["centroid_z"] - b["centroid_z"]
        return {
            "object_id_a": a["id"],
            "category_a": a["category"],
            "object_id_b": b["id"],
            "category_b": b["category"],
            "distance_m": round((dx * dx + dy * dy + dz * dz) ** 0.5, 3),
        }

    def get_category_bounding_box(self, category: str, inspection_id: int | None = None) -> dict[str, Any] | None:
        """Axis-aligned 3D bounding box of all objects in a category."""
        conn = self._connect()
        scope = ""
        params: list[Any] = [category]
        if inspection_id is not None:
            scope = " AND o.id IN (SELECT d.object_id FROM detections d JOIN images i ON i.id=d.image_id WHERE i.inspection_id=?)"
            params.append(inspection_id)
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS count,
                   MIN(o.centroid_x) AS min_cx, MAX(o.centroid_x) AS max_cx,
                   MIN(o.centroid_y) AS min_cy, MAX(o.centroid_y) AS max_cy,
                   MIN(o.centroid_z) AS min_cz, MAX(o.centroid_z) AS max_cz,
                   MIN(o.min_x) AS min_x, MAX(o.max_x) AS max_x,
                   MIN(o.min_y) AS min_y, MAX(o.max_y) AS max_y,
                   MIN(o.min_z) AS min_z, MAX(o.max_z) AS max_z
            FROM objects o JOIN categories c ON c.id=o.category_id
            WHERE c.name = ?{scope}
            """,
            tuple(params),
        ).fetchone()
        if row is None or row["count"] == 0:
            return None
        return dict(row)

    def get_category_detection_timeline(
        self, category: str, bucket_seconds: int = 60, inspection_id: int | None = None
    ) -> list[dict[str, Any]]:
        """Per-time-bucket detection counts for a category."""
        conn = self._connect()
        bucket_ns = int(bucket_seconds * 1e9)
        scope = " AND i.inspection_id=?" if inspection_id is not None else ""
        params: list[Any] = [bucket_ns, bucket_ns, category]
        if inspection_id is not None:
            params.append(inspection_id)
        rows = conn.execute(
            f"""
            SELECT (i.timestamp_ns / ?) * ? AS bucket_ns, COUNT(*) AS count
            FROM detections d
            JOIN images i ON i.id=d.image_id
            JOIN objects o ON o.id=d.object_id
            JOIN categories c ON c.id=o.category_id
            WHERE c.name = ? AND i.timestamp_ns IS NOT NULL{scope}
            GROUP BY bucket_ns
            ORDER BY bucket_ns
            """,
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_objects_by_category_in_time_range(
        self,
        category: str,
        start_time: str | int | float,
        end_time: str | int | float,
        limit: int | str | None = None,
        inspection_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Objects of a category whose detection span overlaps a time window."""
        start_ns = self._parse_time_string(start_time)
        end_ns = self._parse_time_string(end_time)
        if start_ns is None or end_ns is None:
            return []
        where = (
            "c.name = ? AND "
            "(SELECT MIN(i.timestamp_ns) FROM detections d JOIN images i ON i.id=d.image_id "
            "WHERE d.object_id=o.id) <= ? AND "
            "(SELECT MAX(i.timestamp_ns) FROM detections d JOIN images i ON i.id=d.image_id "
            "WHERE d.object_id=o.id) >= ?"
        )
        rows = self._object_rows(
            where, (category, end_ns, start_ns), order="first_seen_ns", limit=self._resolve_limit(limit), inspection_id=inspection_id
        )
        return [dict(row) for row in rows]

    def get_object_movement(self, object_id: int) -> list[dict[str, Any]]:
        """Centroid path of an object across its detections."""
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT i.timestamp_ns, d.centroid_x, d.centroid_y, d.centroid_z, i.filename
            FROM detections d JOIN images i ON i.id=d.image_id
            WHERE d.object_id = ?
            ORDER BY i.timestamp_ns
            """,
            (object_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_nearest_objects_to_object(
        self, object_id: int, radius_m: float = 2.0, inspection_id: int | None = None
    ) -> list[dict[str, Any]]:
        """Other objects within radius_m of an object's centroid."""
        conn = self._connect()
        target = conn.execute(
            "SELECT centroid_x, centroid_y, centroid_z FROM objects WHERE id=?",
            (object_id,),
        ).fetchone()
        if target is None or target["centroid_x"] is None:
            return []
        tx, ty, tz = target["centroid_x"], target["centroid_y"], target["centroid_z"]
        rows = self._object_rows("o.id != ?", (object_id,), inspection_id=inspection_id)
        results = []
        for row in rows:
            if row["centroid_x"] is None:
                continue
            dx = row["centroid_x"] - tx
            dy = row["centroid_y"] - ty
            dz = row["centroid_z"] - tz
            dist = (dx * dx + dy * dy + dz * dz) ** 0.5
            if dist <= radius_m:
                results.append({**dict(row), "distance_m": round(dist, 3)})
        results.sort(key=lambda r: r["distance_m"])
        return results

    def get_images_in_time_range(
        self,
        start_time: str | int | float,
        end_time: str | int | float,
        category: str | None = None,
        limit: int | str | None = None,
        inspection_id: int | None = None,
    ) -> list[str]:
        """Representative image URLs captured in a time window, optionally filtered by category."""
        limit = self._resolve_limit(limit)
        start_ns = self._parse_time_string(start_time)
        end_ns = self._parse_time_string(end_time)
        if start_ns is None or end_ns is None:
            return []
        conn = self._connect()
        sql = """
            SELECT i.filename, MIN(i.timestamp_ns) AS first_seen
            FROM images i
            WHERE i.timestamp_ns >= ? AND i.timestamp_ns <= ? AND i.filename IS NOT NULL
        """
        params: list[Any] = [start_ns, end_ns]
        if category:
            sql += " AND i.id IN (SELECT d.image_id FROM detections d JOIN objects o ON o.id=d.object_id JOIN categories c ON c.id=o.category_id WHERE c.name=?)"
            params.append(category)
        if inspection_id is not None:
            sql += " AND i.inspection_id=?"
            params.append(inspection_id)
        sql += " GROUP BY i.filename ORDER BY first_seen ASC"
        rows = conn.execute(sql, tuple(params)).fetchall()
        distinct = [self._image_url(row["filename"]) for row in rows if row["filename"]]
        distinct = [d for d in distinct if d]
        if not distinct:
            return []
        if limit is None:
            return distinct
        if len(distinct) <= limit:
            return distinct
        step = max(1, (len(distinct) - 1) / (limit - 1))
        sampled: list[str] = []
        seen: set[str] = set()
        for i in range(limit):
            idx = min(int(round(i * step)), len(distinct) - 1)
            path = distinct[idx]
            if path not in seen:
                sampled.append(path)
                seen.add(path)
        return sampled

    def get_category_cooccurrence(
        self, window_ms: int = 500, top_n: int = 10, inspection_id: int | None = None
    ) -> list[dict[str, Any]]:
        """Count how often pairs of categories appear in the same temporal cluster."""
        clusters = self.get_temporal_clusters(window_ms=window_ms, top_n=10000, inspection_id=inspection_id)
        pair_counts: dict[tuple[str, str], int] = {}
        for cluster in clusters:
            cats = sorted(cluster["categories"].keys())
            for i, a in enumerate(cats):
                for b in cats[i + 1:]:
                    key = (a, b)
                    pair_counts[key] = pair_counts.get(key, 0) + 1
        sorted_pairs = sorted(pair_counts.items(), key=lambda item: -item[1])[:top_n]
        return [{"category_a": a, "category_b": b, "cluster_count": count} for (a, b), count in sorted_pairs]

    def get_objects_in_temporal_cluster(
        self,
        center_time: str | int | float,
        window_ms: int = 500,
        limit: int | str | None = None,
        inspection_id: int | None = None,
    ) -> dict[str, Any]:
        """Objects with coordinates detected around a specific time."""
        limit = self._resolve_limit(limit)
        center_ns = self._parse_time_string(center_time)
        if center_ns is None:
            return {"center_time": center_time, "objects": [], "detections": [], "category_counts": {}}
        window_ns = window_ms * 1_000_000
        start_ns = center_ns - window_ns // 2
        end_ns = center_ns + window_ns // 2
        conn = self._connect()
        scope = " AND i.inspection_id=?" if inspection_id is not None else ""
        det_params: list[Any] = [start_ns, end_ns]
        if inspection_id is not None:
            det_params.append(inspection_id)
        limit_clause = " LIMIT ?" if limit is not None else ""
        if limit is not None:
            det_params.append(limit)
        det_rows = conn.execute(
            f"""
            SELECT i.timestamp_ns, d.object_id, c.name AS category,
                   d.centroid_x, d.centroid_y, d.centroid_z, i.filename
            FROM detections d
            JOIN images i ON i.id=d.image_id
            JOIN objects o ON o.id=d.object_id
            JOIN categories c ON c.id=o.category_id
            WHERE i.timestamp_ns >= ? AND i.timestamp_ns <= ?{scope}
            ORDER BY i.timestamp_ns{limit_clause}
            """,
            tuple(det_params),
        ).fetchall()
        detections = [dict(row) for row in det_rows]

        where = (
            "(SELECT MIN(i.timestamp_ns) FROM detections d JOIN images i ON i.id=d.image_id "
            "WHERE d.object_id=o.id) <= ? AND "
            "(SELECT MAX(i.timestamp_ns) FROM detections d JOIN images i ON i.id=d.image_id "
            "WHERE d.object_id=o.id) >= ?"
        )
        obj_rows = self._object_rows(where, (end_ns, start_ns), order="first_seen_ns", limit=limit, inspection_id=inspection_id)
        objects = [dict(row) for row in obj_rows]

        category_counts: dict[str, int] = {}
        for det in detections:
            cat = det.get("category")
            if cat:
                category_counts[cat] = category_counts.get(cat, 0) + 1

        return {
            "center_time_ns": center_ns,
            "window_ms": window_ms,
            "start_time_ns": start_ns,
            "end_time_ns": end_ns,
            "category_counts": category_counts,
            "objects": objects,
            "detections": detections,
        }

    # ------------------------------------------------------------------
    # Anomaly queries (guarded — tables are added by the writer later)
    # ------------------------------------------------------------------

    def get_anomaly_types(self) -> list[str]:
        if not self._anomaly_tables_exist():
            return []
        conn = self._connect()
        rows = conn.execute("SELECT name FROM anomaly_types ORDER BY name").fetchall()
        return [row["name"] for row in rows]

    def get_anomaly_summary(self, inspection_id: int | None = None) -> dict[str, Any]:
        if not self._anomaly_tables_exist():
            return {"available": False}
        conn = self._connect()
        scope = " WHERE i.inspection_id=?" if inspection_id is not None else ""
        scope_params: tuple[Any, ...] = (inspection_id,) if inspection_id is not None else ()
        total_pairs = conn.execute(
            "SELECT COUNT(*) FROM abnormal_detections"
            + (" WHERE inspection_image IN (SELECT id FROM images WHERE inspection_id=?)"
               if inspection_id is not None else ""),
            scope_params,
        ).fetchone()[0]
        by_type = conn.execute(
            f"""
            SELECT t.name AS type, COUNT(ab.id) AS count
            FROM abnormalities ab
            JOIN anomaly_types t ON t.id=ab.type
            JOIN abnormal_detections ad ON ad.id=ab.pair
            JOIN images i ON i.id=ad.inspection_image
            {scope}
            GROUP BY t.name ORDER BY count DESC
            """,
            scope_params,
        ).fetchall()
        by_inspection = conn.execute(
            """
            SELECT i.inspection_id, COUNT(ab.id) AS count
            FROM abnormalities ab
            JOIN abnormal_detections ad ON ad.id=ab.pair
            JOIN images i ON i.id=ad.inspection_image
            GROUP BY i.inspection_id ORDER BY i.inspection_id
            """
        ).fetchall()
        return {
            "available": True,
            "total_pairs": total_pairs,
            "total_abnormalities": sum(r["count"] for r in by_type),
            "by_type": [{"type": r["type"], "count": r["count"]} for r in by_type],
            "by_inspection": [{"inspection_id": r["inspection_id"], "count": r["count"]} for r in by_inspection],
        }

    def get_anomalies(
        self,
        anomaly_id: int | None = None,
        anomaly_type: str | None = None,
        inspection_id: int | None = None,
        limit: int | str | None = None,
    ) -> list[dict[str, Any]]:
        limit = self._resolve_limit(limit)
        if not self._anomaly_tables_exist():
            return []
        conn = self._connect()
        clauses = []
        params: list[Any] = []
        if anomaly_id is not None:
            clauses.append("ab.id = ?")
            params.append(anomaly_id)
        if anomaly_type:
            clauses.append("t.name = ?")
            params.append(anomaly_type)
        if inspection_id is not None:
            clauses.append("ii.inspection_id = ?")
            params.append(inspection_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        limit_clause = " LIMIT ?" if limit is not None else ""
        if limit is not None:
            params.append(limit)
        rows = conn.execute(
            f"""
            SELECT ab.id, t.name AS type, ab.object, ab.location,
                   ab.min_x, ab.min_y, ab.max_x, ab.max_y, ab.note,
                   ad.id AS pair_id, ad.gt_image, ad.inspection_image, ad.summary AS pair_summary,
                   gi.filename AS gt_filename, ii.filename AS inspection_filename,
                   ii.inspection_id,
                   ii.tf_translation_x AS cam_x, ii.tf_translation_y AS cam_y, ii.tf_translation_z AS cam_z
            FROM abnormalities ab
            JOIN anomaly_types t ON t.id=ab.type
            JOIN abnormal_detections ad ON ad.id=ab.pair
            JOIN images gi ON gi.id=ad.gt_image
            JOIN images ii ON ii.id=ad.inspection_image
            {where}
            ORDER BY ab.id{limit_clause}
            """,
            tuple(params),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            d = dict(row)
            d["gt_image_url"] = self._image_url(row["gt_filename"])
            d["inspection_image_url"] = self._image_url(row["inspection_filename"])
            out.append(d)
        return out

    def get_anomaly_locations(
        self,
        inspection_id: int | None = None,
        anomaly_id: int | None = None,
        anomaly_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """3D location of each anomaly, derived from the camera pose of the images.

        Every abnormal pair links a ground-truth image (``gt_image``) and an
        inspection image (``inspection_image``) to the ``images`` table, which stores
        the camera pose (``tf_translation_x/y/z``) where that frame was taken. The
        inspection frame's position is where the robot observed the anomaly; the
        ground-truth frame's position is the reference viewpoint of the same spot.
        Returns one row per individual abnormality (``abnormalities.id``), so the
        user/LLM can address anomalies by their abnormality id rather than the
        internal pair id.  Multiple abnormalities that share the same image pair
        appear as separate rows with the same camera position.
        """
        if not self._anomaly_tables_exist():
            return []
        conn = self._connect()
        clauses: list[str] = []
        params: list[Any] = []
        if inspection_id is not None:
            clauses.append("ii.inspection_id = ?")
            params.append(inspection_id)
        if anomaly_id is not None:
            clauses.append("ab.id = ?")
            params.append(anomaly_id)
        if anomaly_type:
            clauses.append("t.name = ?")
            params.append(anomaly_type)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = conn.execute(
            f"""
            SELECT ab.id AS abnormality_id,
                   ad.id AS pair_id,
                   ii.inspection_id,
                   ii.tf_translation_x AS cam_x, ii.tf_translation_y AS cam_y, ii.tf_translation_z AS cam_z,
                   gi.tf_translation_x AS gt_x, gi.tf_translation_y AS gt_y, gi.tf_translation_z AS gt_z,
                   t.name AS type, ab.object, ab.note
            FROM abnormalities ab
            JOIN abnormal_detections ad ON ad.id = ab.pair
            JOIN anomaly_types t ON t.id = ab.type
            JOIN images ii ON ii.id = ad.inspection_image
            JOIN images gi ON gi.id = ad.gt_image
            {where}
            ORDER BY ab.id
            """,
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def anomaly_location_points(
        self,
        inspection_id: int | None = None,
        anomaly_id: int | None = None,
        anomaly_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Anomaly markers as Rerun coordinate dicts: {x, y, z, label, color} (orange).

        Because ``get_anomaly_locations`` now returns one row per abnormality,
        we group rows by their image pair so each camera position gets a single
        Rerun point with a combined label listing every abnormality in that pair.
        """
        # Group by pair so pairs with multiple abnormalities produce one point.
        by_pair: dict[int, dict[str, Any]] = {}
        labels_by_pair: dict[int, list[str]] = {}
        for row in self.get_anomaly_locations(
            inspection_id=inspection_id,
            anomaly_id=anomaly_id,
            anomaly_type=anomaly_type,
        ):
            pair_id = int(row["pair_id"])
            if pair_id not in by_pair:
                by_pair[pair_id] = dict(row)
                labels_by_pair[pair_id] = []
            obj = row.get("object") or "unknown"
            labels_by_pair[pair_id].append(
                f"Anomaly {row['abnormality_id']}: {row.get('type') or 'unknown'}, {obj}"
            )

        points: list[dict[str, Any]] = []
        for pair_id, row in sorted(by_pair.items()):
            x, y, z = row.get("cam_x"), row.get("cam_y"), row.get("cam_z")
            if x is None or y is None or z is None:
                # Fall back to the ground-truth frame's position if the
                # inspection frame has no pose stored.
                x, y, z = row.get("gt_x"), row.get("gt_y"), row.get("gt_z")
            if x is None or y is None or z is None:
                continue
            label = "; ".join(labels_by_pair[pair_id])
            points.append(
                {
                    "x": x,
                    "y": y,
                    "z": z,
                    "label": label,
                    "color": [255, 170, 0, 255],
                    # Larger than ordinary object points (0.15) so anomaly markers
                    # stand out clearly in the viewer.
                    "radius": 0.35,
                }
            )
        return points

    def run_sql_query(self, query: str, limit: int | str | None = 100) -> dict[str, Any]:
        """Execute a read-only SELECT query and return the results."""
        limit = self._resolve_limit(limit, default=100)
        cleaned = query.strip()
        if not cleaned.lower().startswith("select"):
            return {"error": "Only SELECT queries are allowed.", "query": query}
        forbidden = ("insert", "update", "delete", "drop", "alter", "create", "pragma")
        lowered = cleaned.lower()
        for kw in forbidden:
            if kw in lowered:
                return {"error": f"Forbidden keyword detected: {kw}", "query": query}
        try:
            conn = self._connect()
            cursor = conn.execute(cleaned)
            rows = cursor.fetchmany(limit) if limit is not None else cursor.fetchall()
            columns = [desc[0] for desc in cursor.description] if rows else []
            return {
                "columns": columns,
                "rows": [dict(row) for row in rows],
                "row_count": len(rows),
                "query": query,
            }
        except sqlite3.Error as exc:
            return {"error": str(exc), "query": query}

    # ------------------------------------------------------------------
    # Natural language routing
    # ------------------------------------------------------------------

    async def lookup(
        self,
        query: str,
        chat_history: Sequence[tuple[str, str]] | None = None,
        tool_history: Sequence[dict[str, object]] | None = None,
    ) -> str | None:
        """Return a text summary for DB-related queries, or None if unrelated.

        The router LLM is the sole gatekeeper: every non-empty query reaches it, and it
        decides which tools to call (or none, for off-topic chat). Tool calling can run
        for multiple rounds: after each batch of tools executes, the router sees the
        formatted results and may decide to call additional tools, until it has enough
        information or the configured round limit is reached.
        """
        previous_query = self._last_query
        self._last_query = query
        self._last_chat_history = list(chat_history) if chat_history else []
        self._last_highlight_status = None
        self._last_highlight_args = None
        if not query.strip():
            return None

        if self.router is None:
            return None

        # Generic anomaly follow-up short-circuit: if the previous turn already
        # fetched the full anomaly trio and this question is still a generic
        # anomaly question (no new inspection / anomaly id / type), reuse the
        # cached context instead of re-calling the database.
        if (
            self._last_anomaly_trio_context is not None
            and self._is_anomaly_follow_up(query, previous_query)
            and not self._query_introduces_new_anomaly_scope(query)
        ):
            logger.info("Reusing cached anomaly trio context for follow-up: %r", query)
            all_tool_calls = list(self._last_anomaly_trio_calls)
            all_tool_results = list(self._last_anomaly_trio_results)
            prior_results = [
                str(r.get("output", "")) for r in all_tool_results if r.get("output")
            ]
            db_context = self._last_anomaly_trio_context
            self._record_tool_calls(all_tool_calls)
            self._record_tool_results(all_tool_results)
            # Still run the final-pass highlight so follow-ups like "highlight
            # anomaly 4" can refine the viewer without re-querying.
            if self.rerun_visualizer is not None:
                try:
                    decision = await asyncio.to_thread(
                        self.router.decide_highlights,
                        query,
                        db_context,
                        chat_history,
                        tool_history=tool_history,
                    )
                    logger.info("Cached-path final-pass highlight decision: %s", decision)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Cached-path final-pass highlight decision failed: %s", exc)
                    decision = None
                if decision and self._decision_matches_tool_results(decision, all_tool_calls):
                    self._last_highlight_status = self._apply_highlight_decision(decision)
                    logger.info("Cached-path final-pass highlight status: %s", self._last_highlight_status)
                else:
                    if decision:
                        logger.info(
                            "Cached-path final-pass decision mismatched anomaly query; using fallback"
                        )
                    # Fallback: generic anomaly follow-ups should still mark anomaly
                    # locations in the viewer, scoped to whatever the cached turn asked.
                    coords = self._scoped_anomaly_coordinates(all_tool_calls, query)
                    logger.info(
                        "Cached-path fallback: highlighting %d scoped anomaly location(s)",
                        len(coords),
                    )
                    self._last_highlight_status = self._highlight_in_rerun(
                        {
                            "coordinates": coords,
                            "label": "anomaly locations",
                            "keep_existing": False,
                        }
                    )
            return db_context

        max_rounds = getattr(self.settings, "tool_router_max_rounds", 1) if self.settings else 1
        max_rounds = max(1, int(max_rounds))

        all_tool_calls: list[tuple[str, dict[str, Any]]] = []
        all_tool_results: list[dict[str, Any]] = []
        prior_results: list[str] = []

        try:
            for round_idx in range(max_rounds):
                # Run the router in a thread so multi-round router calls don't block the
                # event loop for the entire sequence.
                tool_calls = await asyncio.to_thread(
                    self.router.select_tool,
                    query,
                    chat_history,
                    tool_history,
                    prior_results,
                    all_tool_calls,
                )

                # Filter out calls we already executed this turn (name + normalized args).
                new_calls: list[tuple[str, dict[str, Any]]] = []
                seen_keys = {
                    self._tool_call_key(name, args) for name, args in all_tool_calls
                }
                for name, args in tool_calls:
                    key = self._tool_call_key(name, args)
                    if key not in seen_keys:
                        new_calls.append((name, args))
                        seen_keys.add(key)

                if not new_calls:
                    # Router stopped, repeated calls, or returned nothing actionable.
                    break

                # Execute the new batch of tools.
                round_results: list[str] = []
                for tool_name, args in new_calls:
                    result = await self._execute_tool(tool_name, args, chat_history=chat_history)
                    all_tool_results.append({"name": tool_name, "args": args, "output": result})
                    all_tool_calls.append((tool_name, args))
                    if result:
                        round_results.append(result)

                if not round_results:
                    break
                prior_results.extend(round_results)

            # Safety net: the router LLM is the gatekeeper, but it occasionally returns
            # no tool calls at all, leaving the answering model to invent categories or
            # subtypes. When the query names a real category ("show me the lights",
            # "how many lights"), or is a broad object question ("tell me about all the
            # objects"), fetch the real data deterministically.
            if not all_tool_calls:
                # First try a changed-parameter re-call: a follow-up like
                # "how about within 5m", "show me 10", or a new category should
                # re-run the most recent matching tool with the updated argument,
                # even when the router returned no tools. This catches the common
                # failure where the router thinks the question was already
                # answered.
                recall_calls = self._changed_param_recall(query, tool_history)
                if recall_calls:
                    logger.info(
                        "Router called no tools; running changed-radius re-call %s for query: %r",
                        recall_calls, query,
                    )
                    for name, args in recall_calls:
                        result = await self._execute_tool(name, args, chat_history=chat_history)
                        all_tool_results.append({"name": name, "args": args, "output": result})
                        all_tool_calls.append((name, args))
                        if result:
                            prior_results.append(result)
                else:
                    net_calls = self._safety_net_calls(query)
                    if net_calls:
                        logger.info("Router called no tools; running safety-net calls %s for query: %r", net_calls, query)
                        for name, args in net_calls:
                            result = await self._execute_tool(name, args, chat_history=chat_history)
                            all_tool_results.append({"name": name, "args": args, "output": result})
                            all_tool_calls.append((name, args))
                            if result:
                                prior_results.append(result)

            # Anomaly completeness: for ANY anomaly-related question the answerer always
            # needs the structured anomaly pair — the 3D anomaly coordinates
            # (get_anomaly_locations) and the anomaly details with images
            # (get_anomalies). The router often calls only a subset (e.g. just locations
            # for a 'where' question); top up whichever ones it did not call, preserving
            # any anomaly_id / anomaly_type / inspection_id scope the user asked for.
            if self._anomaly_tables_exist() and self._ANOMALY_QUERY_RE.search(query):
                called_names = {name for name, _ in all_tool_calls}
                anomaly_scope = self._resolve_anomaly_scope(all_tool_calls, query)
                top_up = [
                    (name, dict(anomaly_scope))
                    for name in ("get_anomaly_locations", "get_anomalies")
                    if name not in called_names
                ]
                if top_up:
                    logger.info("Anomaly query top-up: running %s with %s for query: %r", top_up, anomaly_scope, query)
                    for name, args in top_up:
                        result = await self._execute_tool(name, args, chat_history=chat_history)
                        all_tool_results.append({"name": name, "args": args, "output": result})
                        all_tool_calls.append((name, args))
                        if result:
                            prior_results.append(result)

            self._record_tool_calls(all_tool_calls)
            self._record_tool_results(all_tool_results)

            db_context = (
                "\n\n".join(
                    str(r.get("output", "")) for r in all_tool_results if r.get("output")
                )
                if all_tool_results
                else None
            )

            # Cache the full anomaly pair for generic follow-up questions.
            if (
                self._anomaly_tables_exist()
                and self._ANOMALY_QUERY_RE.search(query)
                and db_context
            ):
                called_names = {name for name, _ in all_tool_calls}
                if {
                    "get_anomaly_locations",
                    "get_anomalies",
                }.issubset(called_names):
                    self._last_anomaly_trio_context = db_context
                    self._last_anomaly_trio_calls = list(all_tool_calls)
                    self._last_anomaly_trio_results = list(all_tool_results)

            # Final-pass highlight: after the router stops calling tools, ask it to decide
            # which objects/coordinates to show in the Rerun viewer (replaces the old regex
            # auto-highlight). Skip if the router already pushed an explicit highlight_in_rerun
            # this turn, or if nothing tool-bearing ran.
            highlight_called = any(
                name == "highlight_in_rerun" for name, _ in all_tool_calls
            )
            if (
                not highlight_called
                and self.rerun_visualizer is not None
                and self.router is not None
                and db_context
            ):
                try:
                    decision = await asyncio.to_thread(
                        self.router.decide_highlights,
                        query,
                        db_context,
                        chat_history,
                        tool_history=tool_history,
                    )
                    logger.info("Final-pass highlight decision: %s", decision)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Final-pass highlight decision failed: %s", exc)
                    decision = None
                if decision and self._decision_matches_tool_results(decision, all_tool_calls):
                    self._last_highlight_status = self._apply_highlight_decision(decision)
                    logger.info("Final-pass highlight status: %s", self._last_highlight_status)
                elif decision:
                    logger.info(
                        "Final-pass highlight decision mismatched tool results; using deterministic fallback"
                    )

                # Fallback: if the user asked about objects of a category and the LLM
                # decider still did not highlight anything, ensure the relevant category
                # (or categories for proximity queries) is shown in the 3D viewer. This
                # covers count/list queries, proximity queries, and image-returning tools.
                # ALL relevant tool calls contribute: categories are merged into one set
                # so multi-category questions ("show me all lights and advertisement
                # boards") highlight every category, not just the first tool call.
                if not self._last_highlight_status:
                    merged_categories: list[str] = []
                    merged_coordinates: list[dict[str, Any]] = []
                    merged_object_ids: list[int] = []
                    anomaly_coords_added = False
                    for name, args in all_tool_calls:
                        if name in {
                            "get_category_objects_with_images",
                            "get_category_sample_images",
                            "get_images_in_time_range",
                            "get_objects_by_category",
                            "get_category_objects_coordinates",
                            "get_objects_by_category_in_time_range",
                            "get_category_timeline",
                            "get_category_detection_timeline",
                        }:
                            cat = args.get("category")
                            if cat:
                                canon = self._canonical_category(cat)
                                if canon not in merged_categories:
                                    merged_categories.append(canon)
                        elif name in {"get_category_proximity", "get_category_proximity_with_images"}:
                            # Proximity questions: highlight the SPECIFIC object pairs that are
                            # within the radius, not the whole categories.  Use the image variant
                            # so we get the actual nearby object ids (the count-only variant only
                            # returns category counts).
                            target = args.get("target_category")
                            others = args.get("other_categories") or []
                            radius = float(args.get("radius_m", 2.0))
                            inspection_id = args.get("inspection_id")
                            if target and others:
                                prox = self.get_category_proximity_with_images(
                                    self._canonical_category(target),
                                    [self._canonical_category(c) for c in others],
                                    radius_m=radius,
                                    limit="all",
                                    nearby_limit="all",
                                    inspection_id=inspection_id,
                                )
                                for r in prox:
                                    oid = int(r["object_id"])
                                    if oid not in merged_object_ids:
                                        merged_object_ids.append(oid)
                                    for n in r.get("nearby", []):
                                        nid = int(n["object_id"])
                                        if nid not in merged_object_ids:
                                            merged_object_ids.append(nid)
                        elif name == "get_objects_proximity_with_images":
                            # Object-ID proximity: highlight both the source objects and the
                            # nearby target-category objects that were returned.
                            raw_ids = args.get("object_ids", args.get("object_id", []))
                            if isinstance(raw_ids, (int, float)):
                                raw_ids = [int(raw_ids)]
                            object_ids = [int(i) for i in raw_ids if i is not None]
                            target = self._canonical_category(args.get("target_category"))
                            radius = float(args.get("radius_m", 2.0))
                            inspection_id = args.get("inspection_id")
                            for oid in object_ids:
                                if oid not in merged_object_ids:
                                    merged_object_ids.append(oid)
                            if object_ids and target:
                                prox = self.get_objects_proximity_with_images(
                                    object_ids,
                                    target,
                                    radius_m=radius,
                                    limit="all",
                                    nearby_limit="all",
                                    inspection_id=inspection_id,
                                )
                                for r in prox:
                                    for n in r.get("nearby", []):
                                        nid = int(n["object_id"])
                                        if nid not in merged_object_ids:
                                            merged_object_ids.append(nid)
                        elif name == "get_objects_near_position":
                            # Position queries ("objects within 5m of (x, y, z)"):
                            # highlight the SPECIFIC objects inside the radius, not
                            # just the reference point.
                            x = float(args.get("x", 0.0))
                            y = float(args.get("y", 0.0))
                            z = float(args.get("z", 0.0))
                            radius = float(args.get("radius_m", 2.0))
                            category = args.get("category")
                            if category:
                                category = self._canonical_category(category)
                            inspection_id = args.get("inspection_id")
                            for obj in self.get_objects_near_position(
                                x, y, z, radius, category=category, inspection_id=inspection_id
                            ):
                                oid = int(obj["id"])
                                if oid not in merged_object_ids:
                                    merged_object_ids.append(oid)
                        elif name == "get_nearest_objects_to_object":
                            # Object-proximity query: highlight the other objects
                            # that were returned as near the given object.
                            oid_src = int(args.get("object_id"))
                            radius = float(args.get("radius_m", 2.0))
                            inspection_id = args.get("inspection_id")
                            for obj in self.get_nearest_objects_to_object(
                                oid_src, radius, inspection_id=inspection_id
                            ):
                                nid = int(obj["id"])
                                if nid not in merged_object_ids:
                                    merged_object_ids.append(nid)
                        elif name == "get_category_windows":
                            for c in args.get("categories") or []:
                                if c:
                                    canon = self._canonical_category(c)
                                    if canon not in merged_categories:
                                        merged_categories.append(canon)
                        elif name in {"get_object_by_id", "get_object_timeline", "get_object_image_paths", "get_object_movement"}:
                            oid = args.get("object_id")
                            if oid is not None and int(oid) not in merged_object_ids:
                                merged_object_ids.append(int(oid))
                        elif name in {"get_anomalies", "get_anomaly_summary", "get_anomaly_locations"}:
                            # Anomaly questions: mark the anomaly locations (camera
                            # positions of the abnormal image pairs) in the 3D viewer.
                            # Added once per query even when several anomaly tools ran.
                            if not anomaly_coords_added:
                                anomaly_coords_added = True
                                merged_coordinates.extend(
                                    self._scoped_anomaly_coordinates(all_tool_calls, query)
                                )
                        elif name in {"get_summary", "get_categories"}:
                            # Broad "tell me about all the objects" queries: every
                            # category is relevant, so highlight them all.
                            for c in self.get_categories():
                                if c not in merged_categories:
                                    merged_categories.append(c)
                    if merged_categories or merged_coordinates or merged_object_ids:
                        highlight_args: dict[str, Any] = {}
                        if merged_categories:
                            highlight_args["categories"] = merged_categories
                        if merged_object_ids:
                            highlight_args["object_ids"] = merged_object_ids
                        if merged_coordinates:
                            highlight_args["coordinates"] = merged_coordinates
                            highlight_args["label"] = "anomaly locations"
                        logger.info(
                            "Fallback highlight triggered with categories=%s object_ids=%s coordinates=%d",
                            merged_categories, merged_object_ids, len(merged_coordinates)
                        )
                        # If every contributing tool call was scoped to the same
                        # inspection, scope the highlight too so the viewer marks only
                        # that inspection's objects of the category. When multiple
                        # categories are requested (e.g. 'show me TV and ticket gates'),
                        # leave inspection_id unset so objects from ALL inspections are
                        # highlighted and grouped per inspection in the viewer.
                        if len(merged_categories) <= 1:
                            scoped = {
                                int(args["inspection_id"])
                                for name, args in all_tool_calls
                                if args.get("inspection_id") is not None
                                and name
                                in {
                                    "get_category_objects_with_images",
                                    "get_category_sample_images",
                                    "get_images_in_time_range",
                                    "get_objects_by_category",
                                    "get_category_objects_coordinates",
                                    "get_objects_by_category_in_time_range",
                                    "get_category_timeline",
                                    "get_category_detection_timeline",
                                    "get_category_proximity",
                                    "get_category_proximity_with_images",
                                    "get_category_windows",
                                    "get_objects_near_position",
                                    "get_nearest_objects_to_object",
                                }
                            }
                            if len(scoped) == 1:
                                highlight_args["inspection_id"] = scoped.pop()
                        try:
                            self._last_highlight_status = self._highlight_in_rerun(highlight_args)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("Tool fallback highlight failed: %s", exc)

            return db_context
        except Exception as exc:
            logger.warning("LLM router execution failed: %s", exc)
            return None

    # Matches queries that are clearly about the inspection's objects/categories even
    # when no specific category is named (used by the no-tool-calls safety net).
    _BROAD_OBJECT_QUERY_RE = re.compile(
        r"\b(objects?|categor(?:y|ies)|detections?|detected|found|scene|inspection|seen|saw)\b",
        re.IGNORECASE,
    )

    # Show-intent words: the user wants frames/images, not just counts.
    _SHOW_INTENT_RE = re.compile(
        r"\b(show|see|display|images?|pictures?|photos?|frames?|look)\b",
        re.IGNORECASE,
    )

    # Flexible radius parser. Matches a number followed by a meter unit,
    # optionally preceded by a cue word ("within", "radius", "try", "expand to",
    # "how about", "extend to", "around", "about"). The cue word is optional so
    # a bare "5m" or "5 meters" also matches. Captures the number.
    _RADIUS_RE = re.compile(
        r"(?:\b(?:within|radius|try|expand(?:\s+to)?|how\s+about|extend(?:\s+to)?|around|about)\s+)?"
        r"(\d+(?:\.\d+)?)\s*(?:m\b|meters?\b|metres?\b)",
        re.IGNORECASE,
    )

    # Limit/count cue: "show me 10", "limit 20", "top 15", "first 5",
    # "up to 8". Captures the number.
    _LIMIT_RE = re.compile(
        r"\b(?:limit|top|first|up\s+to|show(?:\s+me)?|display|give\s+me|just)\s+"
        r"(\d+)\b",
        re.IGNORECASE,
    )

    # "more" / "all" as a limit hint (no number captured — handled specially).
    _MORE_RE = re.compile(r"\b(more|all|every|each)\b", re.IGNORECASE)

    # Coordinate tuple in the query: "(x, y, z)" or "x, y, z" with decimals.
    _COORD_RE = re.compile(
        r"\(?\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)?"
    )

    # Explicit viewer/highlight action words: the user wants the 3D visualization
    # acted on, so a repeat request with UNCHANGED parameters still deserves a
    # fresh tool call (the action must be re-applied with current data).
    _ACTION_INTENT_RE = re.compile(
        r"\b(highlight|highlighting|rerun|viewer|visuali[sz](?:e|ation)|"
        r"3d(?:\s+viewer)?|show (?:it|them) in|display (?:it|them) in|mark)\b",
        re.IGNORECASE,
    )

    # Tools whose results feed the final-pass highlight. When the user explicitly
    # asks for a viewer/highlight action and the router returned no tools, re-issue
    # the most recent of these even when no argument changed, so the highlight pass
    # sees fresh data (e.g. "highlight all objects within 5m of the crack" repeating
    # the previous 5m query).
    _ACTION_RECALL_TOOLS = frozenset({
        "get_objects_near_position",
        "get_nearest_objects_to_object",
        "get_category_proximity",
        "get_category_proximity_with_images",
        "get_objects_proximity_with_images",
        "get_objects_by_category",
        "get_category_objects_with_images",
        "get_object_by_id",
        "get_anomaly_locations",
        "get_anomalies",
        "get_anomaly_summary",
        "get_summary",
        "get_categories",
    })

    # Anomaly/report questions: fetch the structured anomaly data, not object counts.
    _ANOMALY_QUERY_RE = re.compile(
        r"\b(anomal(?:y|ies)|abnormal|findings?|issues?|problems?|defects?|report)\b",
        re.IGNORECASE,
    )

    @classmethod
    def _query_is_about_objects(cls, query: str) -> bool:
        return bool(cls._BROAD_OBJECT_QUERY_RE.search(query))

    _ANOMALY_SCOPE_RE = re.compile(
        r"\b(inspection\s*\d+|anomaly\s*\d+|pair\s*\d+|type\s*\d+|"
        r"foreign_object|missing_object|state_change|relocation|content_change|"
        r"crack and structure damage|stain/graffiti)\b",
        re.IGNORECASE,
    )

    # Vague follow-up words that refer back to the previous anomaly discussion.
    _ANOMALY_FOLLOW_UP_RE = re.compile(
        r"\b(them|those|they|it|more|else|detail|specific|which|what about|"
        r"tell me more|go on|continue|explain|why|how)\b",
        re.IGNORECASE,
    )

    @classmethod
    def _query_introduces_new_anomaly_scope(cls, query: str) -> bool:
        """True if the follow-up asks for a specific inspection, anomaly, or type."""
        return bool(cls._ANOMALY_SCOPE_RE.search(query))

    @classmethod
    def _is_anomaly_follow_up(cls, query: str, last_query: str) -> bool:
        """True if the current question is about anomalies or follows an anomaly answer."""
        if cls._ANOMALY_QUERY_RE.search(query):
            return True
        if (
            last_query
            and cls._ANOMALY_QUERY_RE.search(last_query)
            and cls._ANOMALY_FOLLOW_UP_RE.search(query)
        ):
            return True
        return False

    @classmethod
    def _resolve_anomaly_scope(
        cls,
        tool_calls: list[tuple[str, dict[str, Any]]],
        query: str,
    ) -> dict[str, Any]:
        """Extract anomaly scope (anomaly_id/type/inspection_id) from tool calls or query.

        Prefers explicit arguments already chosen by the router, then falls back
        to parsing the user's query so top-up tools stay scoped to the same
        anomaly/anomaly-type/inspection.
        """
        scope: dict[str, Any] = {}
        # Inherit explicit scope from any anomaly tool the router already called.
        for name, args in tool_calls:
            if name in {"get_anomalies", "get_anomaly_locations", "get_anomaly_summary"}:
                for key in ("anomaly_id", "anomaly_type", "inspection_id"):
                    if args.get(key) is not None and key not in scope:
                        scope[key] = args[key]

        # Parse the query for scope the router may not have passed through.
        if "anomaly_id" not in scope:
            m = re.search(r"\banomaly\s*(?:id|#)?\s*(\d+)\b", query, re.IGNORECASE)
            if m:
                scope["anomaly_id"] = int(m.group(1))
        if "anomaly_type" not in scope:
            for atype in (
                "foreign_object",
                "missing_object",
                "state_change",
                "relocation",
                "content_change",
                "crack and structure damage",
                "stain/graffiti",
            ):
                if re.search(rf"\b{re.escape(atype)}\b", query, re.IGNORECASE):
                    scope["anomaly_type"] = atype
                    break
        if "inspection_id" not in scope:
            m = re.search(r"\binspection\s*(?:id)?\s*(\d+)\b", query, re.IGNORECASE)
            if m:
                scope["inspection_id"] = int(m.group(1))

        return scope

    def _mentioned_categories(self, query: str, max_categories: int = 3) -> list[str]:
        """Canonical DB categories explicitly named in the query (longest alias first)."""
        q = query.lower()
        candidates: list[tuple[str, str]] = [
            (alias, canonical) for alias, canonical in self._CATEGORY_ALIASES.items()
        ]
        candidates += [(c.lower(), c) for c in self.get_categories()]
        candidates.sort(key=lambda item: len(item[0]), reverse=True)
        found: list[str] = []
        for needle, canonical in candidates:
            if canonical in found:
                continue
            if re.search(r"\b" + re.escape(needle) + r"s?\b", q):
                found.append(canonical)
                if len(found) >= max_categories:
                    break
        return found

    def _safety_net_calls(self, query: str) -> list[tuple[str, dict[str, Any]]]:
        """Deterministic tool calls when the router returned nothing.

        Category-named queries get that category's real data (images for show-intent,
        object list otherwise); anomaly questions get the structured anomaly data;
        broad object questions get the overall summary and category list. Anything
        else (greetings, chit-chat) stays untouched.
        """
        if self._ANOMALY_QUERY_RE.search(query) and self._anomaly_tables_exist():
            return [("get_anomaly_summary", {}), ("get_anomalies", {"limit": 5})]
        categories = self._mentioned_categories(query)
        if categories:
            if self._SHOW_INTENT_RE.search(query):
                return [
                    ("get_category_objects_with_images", {"category": c, "limit": 5})
                    for c in categories
                ]
            return [("get_objects_by_category", {"category": c}) for c in categories]
        if self._query_is_about_objects(query):
            return [("get_summary", {}), ("get_categories", {})]
        return []

    def _changed_param_recall(
        self,
        query: str,
        tool_history: Sequence[dict[str, object]] | None,
    ) -> list[tuple[str, dict[str, Any]]]:
        """Deterministic re-call when a follow-up changes a tool argument.

        Catches a common router failure: the user asks a follow-up that changes
        a parameter (a new radius, a new limit, a new category, new coordinates)
        and the router returns no tool calls because it thinks the question was
        already answered. This method parses the follow-up query for changed
        values, scans the cross-turn ``tool_history`` for the most recent call
        whose arguments overlap with the parsed changes, and re-issues that call
        with the updated arguments (all other arguments preserved).

        Supported parameter changes:
          - radius_m  : "within 5m", "try 5 meters", "5m", "expand to 5m"
          - limit / n / top_n / nearby_limit : "show me 10", "limit 20", "top 15"
          - "more" / "all" : widens a capped limit (doubles it, or sets to 'all')
          - category / target_category : a category name in the query
          - x, y, z : a "(x, y, z)" coordinate tuple in the query

        Viewer/highlight action requests ("highlight ... in rerun") additionally
        trigger a re-application pass: when the user explicitly asks for a viewer
        action but the router returned no tools, the most recent highlight-feeding
        tool is re-issued even when no argument changed (repeated requests like
        "highlight all objects within 5m of the crack" must still re-run the query
        so the viewer gets current data).

        Returns an empty list when no parameter change is detectable (and no action
        intent), or when no prior matching tool call exists. Only re-calls the MOST
        RECENT matching call (walking tool_history in reverse) so it never floods
        the turn.
        """
        if not tool_history:
            return []

        action_intent = bool(self._ACTION_INTENT_RE.search(query))

        # --- Parse changed values out of the follow-up query ----------------
        changes: dict[str, Any] = {}

        # Radius: any number + meter unit, optionally cued by "within"/"try"/...
        m = self._RADIUS_RE.search(query)
        if m:
            try:
                changes["radius_m"] = float(m.group(1))
            except (TypeError, ValueError):
                pass

        # Limit / n / top_n: a cued integer in the query.
        m = self._LIMIT_RE.search(query)
        if m:
            try:
                n_val = int(m.group(1))
            except (TypeError, ValueError):
                n_val = None
            if n_val is not None:
                # Apply to whichever of these args the prior call used.
                for key in ("limit", "n", "top_n", "nearby_limit"):
                    changes.setdefault(key, n_val)

        # "more" / "all" widen a capped limit. We don't know the new value yet
        # (it depends on the prior call's limit), so record a sentinel and
        # resolve it against the prior call below.
        if self._MORE_RE.search(query):
            changes["__widen_limit__"] = True

        # Coordinates: a (x, y, z) tuple in the query overrides x/y/z.
        m = self._COORD_RE.search(query)
        if m:
            try:
                changes["x"] = float(m.group(1))
                changes["y"] = float(m.group(2))
                changes["z"] = float(m.group(3))
            except (TypeError, ValueError):
                pass

        # Category: a canonical category name in the query. Applies to whichever
        # of category / target_category the prior call used.
        mentioned = self._mentioned_categories(query)
        if mentioned:
            for key in ("category", "target_category"):
                changes.setdefault(key, mentioned[0])

        if not changes and not action_intent:
            return []

        # --- Find the most recent prior call whose args overlap the changes ---
        # Map each change key to the set of tool names that actually accept it,
        # so we only re-call a tool that can meaningfully use the new value.
        arg_to_tools: dict[str, set[str]] = {
            "radius_m": {
                "get_objects_near_position",
                "get_category_proximity",
                "get_category_proximity_with_images",
                "get_objects_proximity_with_images",
                "get_nearest_objects_to_object",
            },
            "limit": {
                "get_objects_by_category", "get_top_objects", "get_recent_objects",
                "get_object_image_paths", "get_category_objects_with_images",
                "get_category_sample_images", "get_images_in_time_range",
                "get_objects_in_time_range", "get_detections_in_time_range",
                "get_objects_by_category_in_time_range",
                "get_objects_in_temporal_cluster", "get_anomalies",
                "get_category_proximity_with_images",
                "get_objects_proximity_with_images", "run_sql_query",
                "query_database", "annotate_image",
            },
            "n": {"get_top_objects"},
            "top_n": {"get_temporal_clusters", "get_category_cooccurrence"},
            "nearby_limit": {
                "get_category_proximity_with_images",
                "get_objects_proximity_with_images",
            },
            "category": {
                "get_objects_by_category", "get_category_timeline",
                "get_category_objects_coordinates", "get_category_objects_with_images",
                "get_category_sample_images", "get_category_detection_timeline",
                "get_objects_by_category_in_time_range",
                "get_category_proximity", "get_category_proximity_with_images",
                "get_objects_near_position", "get_category_bounding_box",
                "annotate_image",
            },
            "target_category": {
                "get_category_proximity", "get_category_proximity_with_images",
                "get_objects_proximity_with_images",
            },
            "x": {"get_objects_near_position"},
            "y": {"get_objects_near_position"},
            "z": {"get_objects_near_position"},
        }

        # Build the set of tools that could accept AT LEAST ONE parsed change.
        candidate_tools: set[str] = set()
        for key in changes:
            if key == "__widen_limit__":
                continue  # sentinel, handled per-call below
            candidate_tools |= arg_to_tools.get(key, set())

        # Only treat "more"/"all" as a real change if a number cue wasn't found;
        # otherwise the explicit limit already covers it.
        widen_only = (len(changes) == 1 and "__widen_limit__" in changes)
        if widen_only:
            candidate_tools |= arg_to_tools["limit"] | arg_to_tools["nearby_limit"]

        if not candidate_tools:
            return []

        for entry in reversed(list(tool_history)):
            for call in entry.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                name = call.get("name")
                if name not in candidate_tools:
                    continue
                args = call.get("args") or {}
                if not isinstance(args, dict):
                    continue

                new_args = dict(args)
                changed_any = False

                # Apply every parsed change that this tool actually accepts.
                for key, new_val in changes.items():
                    if key == "__widen_limit__":
                        continue
                    if name not in arg_to_tools.get(key, set()):
                        continue
                    old_val = args.get(key)
                    # Skip when the value is unchanged.
                    try:
                        if old_val is not None and float(old_val) == float(new_val):
                            continue
                    except (TypeError, ValueError):
                        if old_val == new_val:
                            continue
                    # For category-like args, only override when the prior call
                    # used a different category (otherwise the user is just
                    # referring back to the same one).
                    if key in ("category", "target_category") and old_val == new_val:
                        continue
                    new_args[key] = new_val
                    changed_any = True

                # "more"/"all": widen whichever limit-like arg the call used.
                if "__widen_limit__" in changes:
                    for key in ("limit", "nearby_limit", "n", "top_n"):
                        if key not in args:
                            continue
                        old_val = args.get(key)
                        if old_val in (None, "all"):
                            continue
                        try:
                            old_int = int(old_val)
                        except (TypeError, ValueError):
                            continue
                        # "all" -> set to 'all'; "more" -> double the cap.
                        if re.search(r"\b(all|every|each)\b", query, re.IGNORECASE):
                            new_args[key] = "all"
                        else:
                            new_args[key] = old_int * 2
                        changed_any = True

                if not changed_any:
                    continue
                return [(name, new_args)]

        # Pass 2 (action re-application): the user explicitly asked to highlight /
        # visualize in the viewer, and the router still returned no tools. Re-issue
        # the most recent highlight-feeding tool with its previous arguments so the
        # final-pass highlight can mark the actual objects — even when the request
        # merely repeats a previous query's parameters.
        if action_intent:
            for entry in reversed(list(tool_history)):
                for call in entry.get("tool_calls") or []:
                    if not isinstance(call, dict):
                        continue
                    name = call.get("name")
                    if name not in self._ACTION_RECALL_TOOLS:
                        continue
                    args = call.get("args") or {}
                    if not isinstance(args, dict):
                        continue
                    return [(name, dict(args))]
        return []

    @staticmethod
    def _tool_call_key(name: str, args: dict[str, Any]) -> str:
        """Stable string key for deduplicating tool calls within a turn."""
        return f"{name}:{json.dumps(args, sort_keys=True, default=str)}"

    async def _execute_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        chat_history: Sequence[tuple[str, str]] | None = None,
    ) -> str | None:
        """Execute a tool selected by the LLM router and return its formatted result."""
        try:
            def _inspection_id() -> int | None:
                v = args.get("inspection_id")
                return int(v) if v is not None else None

            if tool_name == "annotate_image":
                image_url = args.get("image_url") or self._resolve_image_reference(
                    self._last_query, chat_history
                )
                object_id = int(args["object_id"]) if args.get("object_id") is not None else None
                category = (
                    self._canonical_category(args["category"])
                    if args.get("category") and image_url is None
                    else None
                )
                result = await self.annotate_image(
                    image_url=image_url,
                    object_id=object_id,
                    category=category,
                    question=args.get("question") or self._last_query,
                    limit=self._resolve_limit(args.get("limit"), default=5),
                )
                return self._format_annotate_image(result)
            if tool_name == "highlight_in_rerun":
                return self._highlight_in_rerun(args)
            if tool_name == "clear_rerun_markings":
                if self.rerun_visualizer is None:
                    status = "Rerun visualization is not configured on this backend."
                else:
                    status = self.rerun_visualizer.clear()
                self._last_highlight_status = status
                self._last_highlight_args = {"clear": True}
                self._record_highlight({"clear": True}, status)
                return status
            if tool_name == "get_summary":
                return self._format_summary(inspection_id=_inspection_id())
            if tool_name == "get_categories":
                return self._format_categories()
            if tool_name == "get_inspections":
                return self._format_inspections()
            if tool_name == "get_object_by_id":
                return self._format_object(int(args["object_id"]))
            if tool_name == "get_objects_by_category":
                return self._format_category(
                    self._canonical_category(args["category"]),
                    limit=self._resolve_limit(args.get("limit"), default=None),
                    inspection_id=_inspection_id(),
                )
            if tool_name == "get_top_objects":
                return self._format_top_objects(n=self._resolve_limit(args.get("n"), default=5), inspection_id=_inspection_id())
            if tool_name == "get_recent_objects":
                return self._format_recent_objects(limit=self._resolve_limit(args.get("limit"), default=5), inspection_id=_inspection_id())
            if tool_name == "get_object_timeline":
                return self._format_object_timeline(int(args["object_id"]))
            if tool_name == "get_object_image_paths":
                return self._format_object_images(int(args["object_id"]))
            if tool_name == "get_category_timeline":
                return self._format_category_timeline(self._canonical_category(args["category"]), inspection_id=_inspection_id())
            if tool_name == "get_category_windows":
                categories = args.get("categories", [])
                if isinstance(categories, str):
                    categories = [categories]
                return self._format_category_windows(
                    [self._canonical_category(c) for c in categories], inspection_id=_inspection_id()
                )
            if tool_name == "get_category_objects_coordinates":
                return self._format_category_coordinates(self._canonical_category(args["category"]), inspection_id=_inspection_id())
            if tool_name == "get_category_objects_with_images":
                return self._format_category_objects_with_images(
                    self._canonical_category(args["category"]),
                    limit=self._resolve_limit(args.get("limit"), default=5),
                    inspection_id=_inspection_id(),
                )
            if tool_name == "get_category_proximity":
                target = self._canonical_category(args["target_category"])
                others = args.get("other_categories", args.get("other_category", []))
                if isinstance(others, str):
                    others = [others]
                radius = float(args.get("radius_m", 2.0))
                return self._format_category_proximity(
                    target, [self._canonical_category(c) for c in others], radius, inspection_id=_inspection_id()
                )
            if tool_name == "get_category_proximity_with_images":
                target = self._canonical_category(args["target_category"])
                others = args.get("other_categories", args.get("other_category", []))
                if isinstance(others, str):
                    others = [others]
                radius = float(args.get("radius_m", 2.0))
                limit = self._resolve_limit(args.get("limit"), default=None)
                nearby_limit = self._resolve_limit(args.get("nearby_limit"), default=None)
                return self._format_category_proximity_with_images(
                    target,
                    [self._canonical_category(c) for c in others],
                    radius,
                    limit=limit,
                    nearby_limit=nearby_limit,
                    inspection_id=_inspection_id(),
                )
            if tool_name == "get_objects_proximity_with_images":
                raw_ids = args.get("object_ids", args.get("object_id", []))
                if isinstance(raw_ids, (int, float)):
                    raw_ids = [int(raw_ids)]
                object_ids = [int(i) for i in raw_ids if i is not None]
                target = self._canonical_category(args["target_category"])
                radius = float(args.get("radius_m", 2.0))
                limit = self._resolve_limit(args.get("limit"), default=None)
                nearby_limit = self._resolve_limit(args.get("nearby_limit"), default=None)
                return self._format_objects_proximity_with_images(
                    object_ids,
                    target,
                    radius,
                    limit=limit,
                    nearby_limit=nearby_limit,
                    inspection_id=_inspection_id(),
                )
            if tool_name == "get_inspection_timeline":
                return self._format_inspection_timeline(inspection_id=_inspection_id())
            if tool_name == "get_temporal_clusters":
                return self._format_temporal_clusters(
                    window_ms=int(args.get("window_ms", 500)),
                    top_n=int(args.get("top_n", 10)),
                    inspection_id=_inspection_id(),
                )
            if tool_name == "get_detection_counts_by_category":
                return self._format_detection_counts_by_category(inspection_id=_inspection_id())
            if tool_name == "get_objects_in_time_range":
                return self._format_objects_in_time_range(
                    args["start_time"], args["end_time"], limit=self._resolve_limit(args.get("limit"), default=None), inspection_id=_inspection_id()
                )
            if tool_name == "get_detections_in_time_range":
                return self._format_detections_in_time_range(
                    args["start_time"], args["end_time"], limit=self._resolve_limit(args.get("limit"), default=None), inspection_id=_inspection_id()
                )
            if tool_name == "get_objects_in_image":
                return self._format_objects_in_image(args.get("filename") or "")
            if tool_name == "get_objects_near_position":
                return self._format_objects_near_position(
                    x=float(args["x"]),
                    y=float(args["y"]),
                    z=float(args["z"]),
                    radius_m=float(args.get("radius_m", 2.0)),
                    category=self._canonical_category(args["category"]) if args.get("category") else None,
                    inspection_id=_inspection_id(),
                )
            if tool_name == "get_category_sample_images":
                return self._format_category_sample_images(
                    self._canonical_category(args["category"]), limit=self._resolve_limit(args.get("limit"), default=5), inspection_id=_inspection_id()
                )
            if tool_name == "get_inspection_poses":
                return self._format_inspection_poses(limit=self._resolve_limit(args.get("limit"), default=None), inspection_id=_inspection_id())
            if tool_name == "get_object_distance":
                return self._format_object_distance(int(args["object_id_a"]), int(args["object_id_b"]))
            if tool_name == "get_category_bounding_box":
                return self._format_category_bounding_box(self._canonical_category(args["category"]), inspection_id=_inspection_id())
            if tool_name == "get_category_detection_timeline":
                return self._format_category_detection_timeline(
                    self._canonical_category(args["category"]),
                    bucket_seconds=int(args.get("bucket_seconds", 60)),
                    inspection_id=_inspection_id(),
                )
            if tool_name == "get_objects_by_category_in_time_range":
                return self._format_objects_by_category_in_time_range(
                    self._canonical_category(args["category"]),
                    args["start_time"],
                    args["end_time"],
                    limit=self._resolve_limit(args.get("limit"), default=None),
                    inspection_id=_inspection_id(),
                )
            if tool_name == "get_object_movement":
                return self._format_object_movement(int(args["object_id"]))
            if tool_name == "get_nearest_objects_to_object":
                return self._format_nearest_objects_to_object(
                    int(args["object_id"]),
                    radius_m=float(args.get("radius_m", 2.0)),
                    inspection_id=_inspection_id(),
                )
            if tool_name == "get_images_in_time_range":
                return self._format_images_in_time_range(
                    args["start_time"],
                    args["end_time"],
                    category=self._canonical_category(args["category"]) if args.get("category") else None,
                    limit=self._resolve_limit(args.get("limit"), default=None),
                    inspection_id=_inspection_id(),
                )
            if tool_name == "get_category_cooccurrence":
                return self._format_category_cooccurrence(
                    window_ms=int(args.get("window_ms", 500)),
                    top_n=int(args.get("top_n", 10)),
                    inspection_id=_inspection_id(),
                )
            if tool_name == "get_objects_in_temporal_cluster":
                return self._format_objects_in_temporal_cluster(
                    args["center_time"],
                    window_ms=int(args.get("window_ms", 500)),
                    limit=self._resolve_limit(args.get("limit"), default=None),
                    inspection_id=_inspection_id(),
                )
            if tool_name in {"run_sql_query", "query_database"}:
                sql = args.get("query") or args.get("sql_query") or ""
                return self._format_sql_query_result(sql, limit=self._resolve_limit(args.get("limit"), default=100))
            if tool_name == "get_anomaly_types":
                return self._format_anomaly_types()
            if tool_name == "get_anomaly_summary":
                return self._format_anomaly_summary(inspection_id=_inspection_id())
            if tool_name == "get_anomalies":
                raw_anomaly_id = args.get("anomaly_id")
                return self._format_anomalies(
                    anomaly_id=int(raw_anomaly_id) if raw_anomaly_id is not None else None,
                    anomaly_type=args.get("anomaly_type"),
                    inspection_id=_inspection_id(),
                    limit=self._resolve_limit(args.get("limit"), default=None),
                )
            if tool_name == "get_anomaly_locations":
                return self._format_anomaly_locations(inspection_id=_inspection_id())
        except Exception as exc:
            logger.warning("Tool execution failed for %s with args %s: %s", tool_name, args, exc)
        return None

    def _enrich_coordinate_labels(self, coords: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Rewrite labels at known anomaly camera positions to rich labels.

        Matches coordinates against the camera positions returned by
        ``anomaly_location_points``; when a coordinate falls on an anomaly
        viewpoint, its label is replaced with the canonical form
        'Anomaly {abnormality_id}: {type}, {object}' (multiple abnormalities in
        the same pair are joined with '; ').
        """
        if not coords or not self._anomaly_tables_exist():
            return list(coords)
        anomaly_points = self.anomaly_location_points()
        if not anomaly_points:
            return list(coords)
        # Build (x,y,z) -> rich label map with a small tolerance for float noise.
        label_map: dict[tuple[int, int, int], str] = {}
        for p in anomaly_points:
            key = (round(float(p["x"]) * 100), round(float(p["y"]) * 100), round(float(p["z"]) * 100))
            label_map[key] = p.get("label", "")

        out: list[dict[str, Any]] = []
        for c in coords:
            if not isinstance(c, dict):
                out.append(c)
                continue
            enriched = dict(c)
            try:
                key = (
                    round(float(c["x"]) * 100),
                    round(float(c["y"]) * 100),
                    round(float(c["z"]) * 100),
                )
                rich = label_map.get(key)
                if rich:
                    enriched["label"] = rich
            except (KeyError, TypeError, ValueError):
                pass
            out.append(enriched)
        return out

    def _scoped_anomaly_coordinates(
        self,
        tool_calls: list[tuple[str, dict[str, Any]]],
        query: str,
    ) -> list[dict[str, Any]]:
        """Anomaly coordinates filtered to the scope the user asked for.

        Uses explicit anomaly tool arguments when available, then falls back to
        parsing the query.  This prevents a single-anomaly question from lighting
        up every anomaly location in the viewer.
        """
        scope = self._resolve_anomaly_scope(tool_calls, query)
        return self.anomaly_location_points(
            inspection_id=scope.get("inspection_id"),
            anomaly_id=scope.get("anomaly_id"),
            anomaly_type=scope.get("anomaly_type"),
        )

    def _highlight_in_rerun(self, args: dict[str, Any]) -> str:
        object_ids = args.get("object_ids") or []
        if isinstance(object_ids, (int, float)):
            object_ids = [int(object_ids)]
        object_ids = [int(o) for o in object_ids if o is not None]
        coords = self._enrich_coordinate_labels(args.get("coordinates") or [])
        category = self._canonical_category(args["category"]) if args.get("category") else None
        categories = [
            self._canonical_category(c)
            for c in (args.get("categories") or [])
            if c
        ]
        keep_existing = bool(args.get("keep_existing")) or self._query_wants_keep(self._last_query)
        normalized: dict[str, Any] = {
            "object_ids": object_ids or None,
            "coordinates": coords or None,
            "category": category,
            "categories": categories or None,
            "inspection_id": int(args["inspection_id"]) if args.get("inspection_id") is not None else None,
            "label": args.get("label"),
            "keep_existing": keep_existing,
        }
        if self.rerun_visualizer is None:
            status = "Rerun visualization is not configured on this backend."
            self._last_highlight_args = normalized
            self._last_highlight_status = status
            self._record_highlight(normalized, status)
            return status
        status = self.rerun_visualizer.highlight(**normalized)
        self._last_highlight_args = normalized
        self._last_highlight_status = status
        self._record_highlight(normalized, status)
        return status

    # ------------------------------------------------------------------
    # Final-pass Rerun highlighting (decided by the router after tools run)
    # ------------------------------------------------------------------

    # Phrases that explicitly ask to KEEP previous highlights (so we do NOT clear).
    # Conservative on purpose: the router / final-pass LLM also exposes a keep_existing
    # tool flag for nuance; this is just a safety net for obvious cases.
    _KEEP_KEYWORDS = (
        "keep", "don't clear", "dont clear", "don't remove", "dont remove",
        "do not clear", "do not remove", "add to", "alongside", "in addition to",
        "accumulate", "as well as the previous", "as well as the existing",
    )

    @classmethod
    def _query_wants_keep(cls, query: str | None) -> bool:
        """True if the user explicitly asked to keep / add to previous highlights."""
        if not query:
            return False
        q = query.lower()
        return any(kw in q for kw in cls._KEEP_KEYWORDS)

    def _apply_highlight_decision(self, decision: dict[str, Any]) -> str | None:
        """Push a final-pass highlight decision to the Rerun viewer.

        Driven by the router's post-tool ``set_rerun_highlight`` decision (see
        :meth:`ToolRouter.decide_highlights`) instead of an explicit ``highlight_in_rerun``
        tool call. Mirrors :meth:`_highlight_in_rerun` for argument normalization. Returns
        the ``RerunVisualizer`` status string, or ``None`` on failure / nothing to
        highlight. Never raises.
        """
        if self.rerun_visualizer is None:
            return None
        object_ids = decision.get("object_ids") or []
        if isinstance(object_ids, (int, float)):
            object_ids = [int(object_ids)]
        object_ids = [int(o) for o in object_ids if o is not None]
        coords = self._enrich_coordinate_labels(decision.get("coordinates") or [])
        category = (
            self._canonical_category(decision["category"])
            if decision.get("category")
            else None
        )
        categories = [
            self._canonical_category(c)
            for c in (decision.get("categories") or [])
            if c
        ]
        label = decision.get("label") or "final"
        keep_existing = bool(decision.get("keep_existing")) or self._query_wants_keep(self._last_query)
        normalized: dict[str, Any] = {
            "object_ids": object_ids or None,
            "coordinates": coords or None,
            "category": category,
            "categories": categories or None,
            "inspection_id": (
                int(decision["inspection_id"])
                if decision.get("inspection_id") is not None
                else None
            ),
            "label": label,
            "keep_existing": keep_existing,
        }
        try:
            status = self.rerun_visualizer.highlight(**normalized)
            self._last_highlight_args = normalized
            self._last_highlight_status = status
            self._record_highlight(normalized, status)
            return status
        except Exception as exc:  # noqa: BLE001
            logger.warning("Final-pass Rerun highlight failed: %s", exc)
            return None

    def _decision_matches_tool_results(
        self,
        decision: dict[str, Any],
        tool_calls: list[tuple[str, dict[str, Any]]],
    ) -> bool:
        """Return False when the LLM chose a broad category highlight for a query
        that specifically returned proximity or anomaly results.

        Proximity and anomaly questions need precise marks (object ids or anomaly
        camera positions).  A category-level highlight would light up every object
        in those categories, so we reject it and let the deterministic fallback run.
        """
        names = {name for name, _ in tool_calls}
        had_proximity = bool(
            names & {"get_category_proximity", "get_category_proximity_with_images", "get_objects_proximity_with_images"}
        )
        had_anomaly = bool(
            names & {"get_anomalies", "get_anomaly_locations", "get_anomaly_summary"}
        )
        if not (had_proximity or had_anomaly):
            return True

        has_object_ids = bool(decision.get("object_ids"))
        has_coordinates = bool(decision.get("coordinates"))
        has_categories = bool(decision.get("category") or decision.get("categories"))

        if had_proximity:
            # Proximity results list specific nearby object ids; categories are wrong.
            return has_object_ids or has_coordinates
        # Anomaly results are about anomaly camera locations; only coordinate highlights
        # make sense here.  Object ids from the LLM are usually misinterpretations of
        # the abnormality id, and category highlighting is far too broad.
        return has_coordinates

    # ------------------------------------------------------------------
    # Image annotation
    # ------------------------------------------------------------------

    async def annotate_image(
        self,
        image_url: str | None = None,
        object_id: int | None = None,
        category: str | None = None,
        question: str | None = None,
        limit: int | str | None = 5,
    ) -> dict[str, Any] | None:
        """Run vision annotation on one or more inspection images and cache results.

        Resolves *image_url* (a single image), *object_id* (all distinct frames for the
        object, up to ``limit``), or *category* (up to ``limit`` random samples) into
        local image paths, asks the vision model to mark anomalies on each, and saves
        every annotated PNG to the configured cache directory so the frontend can
        request it.
        """
        limit = self._resolve_limit(limit, default=5)
        if not self.vision_annotator:
            return {"error": "Vision annotator is not configured."}

        raw_paths: list[str] = []
        if image_url:
            resolved = self._resolve_image_path(image_url)
            raw_paths = [str(resolved)] if resolved else []
        elif object_id is not None:
            raw_paths = self.get_object_image_paths(object_id)[:limit]
        elif category:
            raw_paths = self.get_category_sample_images(category, limit=limit)

        image_paths: list[Path] = []
        seen: set[str] = set()
        for path_str in raw_paths:
            resolved = self._resolve_image_path(path_str)
            if resolved is None:
                continue
            key = str(resolved)
            if key in seen:
                continue
            seen.add(key)
            image_paths.append(resolved)

        if not image_paths:
            return {"error": f"Could not locate images for annotation from image_url={image_url}, object_id={object_id}, category={category}"}

        q = question or "What anomalies are in this image?"
        cache_dir = self._annotated_image_cache_dir()
        cache_dir.mkdir(parents=True, exist_ok=True)

        images_result: list[dict[str, Any]] = []
        errors: list[str] = []
        for image_path in image_paths:
            if not image_path.exists():
                errors.append(f"{image_path.name}: missing on disk")
                continue
            image_bytes = image_path.read_bytes()
            try:
                result = await self.vision_annotator.annotate(image_bytes, q)
            except Exception as exc:
                logger.warning("Vision annotation failed for %s: %s", image_path, exc)
                errors.append(f"{image_path.name}: {exc}")
                continue

            annotated_bytes = base64.b64decode(result["annotated_image_base64"])
            filename = f"{hashlib.sha256(annotated_bytes).hexdigest()[:16]}.png"
            (cache_dir / filename).write_bytes(annotated_bytes)
            images_result.append({
                "source": image_path.name,
                "description": result.get("description", ""),
                "annotated_image_url": f"/annotated/images/{filename}",
                "annotations": result.get("annotations", []),
                "vision_raw_response": result.get("raw_response", ""),
            })

        if not images_result:
            return {"error": "Vision annotation failed for all images: " + "; ".join(errors)}

        return {
            "images": images_result,
            "count": len(images_result),
            "errors": errors,
        }

    def _resolve_image_path(self, image_url: str | None) -> Path | None:
        """Map a frontend URL or raw path to a local filesystem path."""
        if not image_url:
            return None
        image_url = str(image_url).strip()
        if not image_url:
            return None

        if image_url.startswith("http://") or image_url.startswith("https://"):
            image_url = urlparse(image_url).path

        if image_url.startswith("/inspection/images/"):
            if not self.settings:
                return None
            return Path(self.settings.inspection_image_dir) / Path(image_url).name
        if image_url.startswith("/annotated/images/"):
            if not self.settings:
                return None
            return Path(self.settings.annotated_image_cache_dir) / Path(image_url).name

        raw = Path(image_url)
        if raw.is_absolute() and raw.exists():
            return raw

        if self.settings:
            candidates = [
                Path(self.settings.inspection_image_dir) / raw,
            ]
            for candidate in candidates:
                if candidate.exists():
                    return candidate
        return None

    def _annotated_image_cache_dir(self) -> Path:
        if self.settings and getattr(self.settings, "annotated_image_cache_dir", None):
            return Path(self.settings.annotated_image_cache_dir)
        return Path("./annotated_images").resolve()

    def _format_annotate_image(self, result: dict[str, Any] | None) -> str:
        if result is None:
            return "Image annotation returned no result."
        if "error" in result:
            return f"Image annotation failed: {result['error']}"
        images = result.get("images") or []
        if not images:
            return "Image annotation produced no annotated images."
        total = len(images)
        lines: list[str] = [
            f"{total} annotated image(s) are shown below. PRESERVE every image link in your answer.",
            "",
        ]
        raws: list[str] = []
        for idx, im in enumerate(images, start=1):
            lines.append(f"### Annotated image {idx}/{total}")
            lines.append(f"![annotated result]({im['annotated_image_url']})")
            lines.append("")
            lines.append(f"Description: {im.get('description') or 'No description provided.'}")
            anns = im.get("annotations") or []
            if anns:
                lines.append(f"Annotations drawn: {len(anns)}")
            lines.append("")
            raw = im.get("vision_raw_response")
            if raw:
                raws.append(f"[Image {idx}] {raw}")
        if raws:
            lines.append("--- Vision model raw output ---")
            lines.extend(raws)
        errors = result.get("errors") or []
        if errors:
            lines.append("")
            lines.append(f"Skipped {len(errors)} image(s): " + "; ".join(errors))
        return "\n".join(lines).strip()

    # ------------------------------------------------------------------
    # Formatters
    # ------------------------------------------------------------------

    def _format_summary(self, inspection_id: int | None = None) -> str:
        data = self.get_summary(inspection_id=inspection_id)
        scope = f" (inspection {inspection_id})" if inspection_id is not None else ""
        lines = [f"Total objects{scope}: {data['total_objects']}"]
        if data["categories"]:
            lines.append("Objects by category:")
            for row in data["categories"]:
                lines.append(f"- {row['category']}: {row['count']}")
        if inspection_id is None:
            inspections = self.get_inspections()
            if len(inspections) > 1:
                lines.append(
                    "NOTE: the totals above COMBINE all inspections. Per-inspection breakdown "
                    "(each is a separate run — do not blend them when the user asks about 'the inspection'):"
                )
                for ins in inspections:
                    gt = " (ground truth)" if ins["is_gt"] else ""
                    lines.append(
                        f"- Inspection {ins['id']}{gt}: started {ins['started_at']}, "
                        f"{ins['object_count']} object(s), {ins['detection_count']} detection(s)"
                    )
        if data.get("objects"):
            lines.append("Notable objects (by detection count):")
            for obj in data["objects"][:5]:
                lines.append(
                    f"- Object {obj['id']} ({obj['category']}): "
                    f"{obj['detection_count']} detections"
                )
        lines.append("You can ask me about any object ID to see its frames.")
        return "\n".join(lines)

    def _format_categories(self) -> str:
        categories = self.get_categories()
        if not categories:
            return "No categories found in the database."
        return "Known categories:\n" + "\n".join(f"- {c}" for c in categories)

    def _format_inspections(self) -> str:
        inspections = self.get_inspections()
        if not inspections:
            return "No inspections found in the database."
        lines = [f"{len(inspections)} inspection(s):"]
        for ins in inspections:
            gt = " (ground truth)" if ins["is_gt"] else ""
            lines.append(
                f"- Inspection {ins['id']}: started {ins['started_at']}{gt}, "
                f"{ins['object_count']} object(s), {ins['detection_count']} detection(s)"
            )
        lines.append("Pass inspection_id to scope any other tool to a specific inspection.")
        return "\n".join(lines)

    def _format_query_database(self, sql_query: str, limit: int = 100) -> str:
        return self._format_sql_query_result(sql_query, limit)

    def _format_category(self, category: str, limit: int | None = None, inspection_id: int | None = None) -> str:
        objects = self.get_objects_by_category(category, limit=limit, inspection_id=inspection_id)
        if not objects:
            return f"No objects found in category '{category}'."
        scope = f" (inspection {inspection_id})" if inspection_id is not None else ""

        # Total distinct objects in this category (independent of the returned sample limit).
        counts = self._category_counts(inspection_id=inspection_id)
        total = next((c["count"] for c in counts if c["category"] == category), len(objects))

        meta = self._inspection_meta()
        multi = inspection_id is None and len(meta) > 1

        if multi:
            per = self._category_counts_by_inspection(category)
            breakdown = ", ".join(
                f"{self._inspection_tag(row['inspection_id'], meta)}: {row['count']}" for row in per
            )
            header = (
                f"Found {total} object(s) in category '{category}' across {len(meta)} inspections "
                f"({breakdown}). These are SEPARATE counts per inspection run — do not add them up "
                f"unless the user explicitly wants a combined total."
            )
        elif total == len(objects):
            header = f"Found {total} object(s) in category '{category}'{scope}."
        else:
            header = (
                f"Found {total} object(s) in category '{category}'{scope} "
                f"(showing first {len(objects)})."
            )
        lines = [header + " Object IDs:"]
        for obj in objects:
            tag = f" [{self._inspection_tag(obj.get('inspection_id'), meta)}]" if multi else ""
            lines.append(
                f"- Object {obj['id']}{tag}: {obj['detection_count']} detections, "
                f"{self._format_xyz(obj['centroid_x'], obj['centroid_y'], obj['centroid_z'], prefix='centroid ')}"
            )
        lines.append(f"To see frames, say: 'show me images of object {objects[0]['id']}'.")
        return "\n".join(lines)

    def _format_object(self, object_id: int) -> str:
        obj = self.get_object_by_id(object_id)
        if obj is None:
            return f"No object found with object ID {object_id}."
        meta = self._inspection_meta()
        lines = [
            f"Object ID: {obj['id']}",
            f"Category: {obj['category']}",
            f"Inspection: {self._inspection_tag(obj.get('inspection_id'), meta)}",
            f"Detections: {obj['detection_count']}",
            f"Centroid: {self._format_xyz(obj['centroid_x'], obj['centroid_y'], obj['centroid_z'])}",
            f"First seen: {self._format_timestamp(obj.get('first_seen_ns'))}",
            f"Last seen: {self._format_timestamp(obj.get('last_seen_ns'))}",
        ]
        det_count = len(obj.get("detections", []))
        if det_count:
            lines.append(f"Per-frame detection rows: {det_count}")
        return "\n".join(lines)

    def _format_top_objects(self, n: int = 5, inspection_id: int | None = None) -> str:
        objects = self.get_top_objects(n, inspection_id=inspection_id)
        if not objects:
            return "No objects found in the database."
        meta = self._inspection_meta()
        multi = inspection_id is None and len(meta) > 1
        lines = [f"Top {len(objects)} objects by detection count. Object IDs:"]
        for obj in objects:
            tag = f" [{self._inspection_tag(obj.get('inspection_id'), meta)}]" if multi else ""
            lines.append(
                f"- Object {obj['id']} ({obj['category']}){tag}: "
                f"{obj['detection_count']} detections"
            )
        lines.append("To see frames for an object, ask: 'show me images of object [ID]'.")
        return "\n".join(lines)

    def _format_recent_objects(self, limit: int = 5, inspection_id: int | None = None) -> str:
        objects = self.get_recent_objects(limit, inspection_id=inspection_id)
        if not objects:
            return "No objects found in the database."
        meta = self._inspection_meta()
        multi = inspection_id is None and len(meta) > 1
        lines = [f"{len(objects)} most recently seen object(s). Object IDs:"]
        for obj in objects:
            tag = f" [{self._inspection_tag(obj.get('inspection_id'), meta)}]" if multi else ""
            lines.append(
                f"- Object {obj['id']} ({obj['category']}){tag}: "
                f"last seen at {self._format_timestamp(obj['last_seen_ns'])}"
            )
        lines.append("To see frames, ask: 'show me images of object [ID]'.")
        return "\n".join(lines)

    def _format_category_coordinates(self, category: str, inspection_id: int | None = None) -> str:
        objects = self.get_category_objects_with_coordinates(category, inspection_id=inspection_id)
        if not objects:
            return f"No objects found in category '{category}'."
        lines = [f"Objects in category '{category}' with coordinates (ordered by first appearance). Object IDs:"]
        for obj in objects:
            lines.append(
                f"- Object {obj['id']}: first seen {self._format_timestamp(obj['first_seen_ns'])}, "
                f"{self._format_xyz(obj['centroid_x'], obj['centroid_y'], obj['centroid_z'], prefix='centroid ')}"
            )
        lines.append("Ask: 'show me images of object [ID]' to view frames.")
        return "\n".join(lines)

    def _format_category_objects_with_images(self, category: str, limit: int | None = None, inspection_id: int | None = None) -> str:
        objects = self.get_category_objects_with_images(category, limit, inspection_id=inspection_id)
        if not objects:
            return f"No objects found in category '{category}'."
        total = next(
            (c["count"] for c in self._category_counts(inspection_id) if c["category"] == category),
            len(objects),
        )
        is_subset = len(objects) < total
        scope_note = (
            f" (representative subset: showing {len(objects)} of {total} '{category}' objects)"
            if is_subset
            else f" (all {len(objects)} object(s))"
        )
        lines = [
            f"Objects in category '{category}' with object IDs, coordinates, and sample images{scope_note}:"
        ]
        for obj in objects:
            lines.append(
                f"- Object {obj['id']}: "
                f"{self._format_xyz(obj['centroid_x'], obj['centroid_y'], obj['centroid_z'], prefix='centroid ')}"
                f", first seen {self._format_timestamp(obj['first_seen_ns'])}"
            )
            sample_url = self._image_url(obj.get("sample_image_path"))
            if sample_url:
                lines.append(f"  ![sample frame]({sample_url})")
        if is_subset:
            lines.append(
                f"These are a representative subset of the '{category}' objects — "
                "ask if you'd like to see more images."
            )
        lines.append("To see more frames for an object, ask: 'show me images of object [ID]'.")
        return "\n".join(lines)

    def _format_category_proximity(
        self,
        target_category: str,
        other_categories: list[str],
        radius_m: float = 2.0,
        inspection_id: int | None = None,
    ) -> str:
        results = self.get_category_proximity(target_category, other_categories, radius_m, inspection_id=inspection_id)
        if not results:
            return f"No proximity data found for '{target_category}' near {other_categories}."

        aggregate: dict[str, int] = {}
        detailed_lines = []
        for r in results:
            nearby = r["nearby"]
            for cat, cnt in nearby.items():
                aggregate[cat] = aggregate.get(cat, 0) + cnt
            nearby_str = ", ".join(f"{cnt} x {cat}" for cat, cnt in sorted(nearby.items(), key=lambda x: -x[1]))
            detailed_lines.append(
                f"- Object {r['object_id']} at "
                f"{self._format_xyz(r['centroid_x'], r['centroid_y'], r['centroid_z'])}: "
                f"within {radius_m} m — {nearby_str or 'nothing nearby'}"
            )

        summary = ", ".join(f"{cnt} x {cat}" for cat, cnt in sorted(aggregate.items(), key=lambda x: -x[1]))
        scope = f" (inspection {inspection_id})" if inspection_id is not None else ""
        lines = [
            f"Proximity summary for '{target_category}'{scope} within {radius_m} m of {', '.join(other_categories)}:",
            f"Total nearby objects: {summary or 'none'}",
            "Per-target breakdown:",
        ]
        lines.extend(detailed_lines)
        return "\n".join(lines)

    def _format_category_proximity_with_images(
        self,
        target_category: str,
        other_categories: list[str],
        radius_m: float = 2.0,
        limit: int | None = None,
        nearby_limit: int | None = None,
        inspection_id: int | None = None,
    ) -> str:
        results = self.get_category_proximity_with_images(
            target_category,
            other_categories,
            radius_m,
            limit=limit,
            nearby_limit=nearby_limit,
            inspection_id=inspection_id,
        )
        if not results:
            return f"No '{target_category}' objects found near {other_categories} within {radius_m} m."

        scope = f" (inspection {inspection_id})" if inspection_id is not None else ""
        lines = [
            f"Nearby objects for '{target_category}'{scope} within {radius_m} m of {', '.join(other_categories)}, "
            f"with object IDs, distances, coordinates, and sample images:"
        ]
        total_nearby = 0
        for r in results:
            nearby = r["nearby"]
            if not nearby:
                continue
            total_nearby += len(nearby)
            lines.append(
                f"- From Object {r['object_id']} at "
                f"{self._format_xyz(r['centroid_x'], r['centroid_y'], r['centroid_z'])}:"
            )
            for n in nearby:
                lines.append(
                    f"  - Object {n['object_id']} ({n['category']}): "
                    f"{n['distance_m']:.2f} m away at "
                    f"{self._format_xyz(n['centroid_x'], n['centroid_y'], n['centroid_z'])}"
                )
                sample_url = self._image_url(n.get("sample_image_path"))
                if sample_url:
                    lines.append(f"    ![nearby frame]({sample_url})")
        if total_nearby == 0:
            lines.append(f"No {', '.join(other_categories)} objects were found within {radius_m} m of any '{target_category}' object.")
        return "\n".join(lines)

    def _format_objects_proximity_with_images(
        self,
        object_ids: list[int],
        target_category: str,
        radius_m: float = 2.0,
        limit: int | None = None,
        nearby_limit: int | None = None,
        inspection_id: int | None = None,
    ) -> str:
        results = self.get_objects_proximity_with_images(
            object_ids,
            target_category,
            radius_m,
            limit=limit,
            nearby_limit=nearby_limit,
            inspection_id=inspection_id,
        )
        if not results:
            return f"No '{target_category}' objects found near objects {object_ids} within {radius_m} m."

        scope = f" (inspection {inspection_id})" if inspection_id is not None else ""
        lines = [
            f"'{target_category}' objects within {radius_m} m of objects {object_ids}{scope}, "
            f"with object IDs, distances, coordinates, and sample images:"
        ]
        total_nearby = 0
        for r in results:
            nearby = r["nearby"]
            if not nearby:
                continue
            total_nearby += len(nearby)
            lines.append(
                f"- Near Object {r['source_object_id']} "
                f"({r.get('source_category', 'unknown')}) at "
                f"{self._format_xyz(r['centroid_x'], r['centroid_y'], r['centroid_z'])}:"
            )
            for n in nearby:
                lines.append(
                    f"  - Object {n['object_id']} ({n['category']}): "
                    f"{n['distance_m']:.2f} m away at "
                    f"{self._format_xyz(n['centroid_x'], n['centroid_y'], n['centroid_z'])}"
                )
                sample_url = self._image_url(n.get("sample_image_path"))
                if sample_url:
                    lines.append(f"    ![nearby frame]({sample_url})")
        if total_nearby == 0:
            lines.append(
                f"No '{target_category}' objects were found within {radius_m} m of the specified objects."
            )
        return "\n".join(lines)

    def _format_object_timeline(self, object_id: int) -> str:
        obj = self.get_object_by_id(object_id)
        if obj is None:
            return f"No object found with object ID {object_id}."

        first_ns = obj.get("first_seen_ns")
        last_ns = obj.get("last_seen_ns")
        duration_s = ((last_ns - first_ns) / 1e9) if first_ns and last_ns else 0.0

        lines = [
            f"Object {obj['id']} ({obj['category']}) timeline:",
            f"- First seen: {self._format_timestamp(first_ns)}",
            f"- Last seen:  {self._format_timestamp(last_ns)}",
            f"- Detections: {obj['detection_count']}",
            f"- Visible for about {self._format_duration(duration_s)}",
        ]

        detections = obj.get("detections", [])
        if detections:
            lines.append("Key moments:")
            step = max(1, len(detections) // 5)
            for i, det in enumerate(detections):
                if i % step == 0 or i == len(detections) - 1:
                    lines.append(f"  - {self._format_timestamp(det['timestamp_ns'])}: frame {det['filename']}")

        image_urls = self.get_object_image_paths(object_id)
        if image_urls:
            lines.append("Object frames:")
            for url in image_urls[:10]:
                lines.append(f"![frame]({url})")
            lines.append(f"To view all frames for this object, ask: 'show me all images of object {object_id}'.")
        return "\n".join(lines)

    def _format_object_images(self, object_id: int, limit: int = 10) -> str:
        obj = self.get_object_by_id(object_id)
        if obj is None:
            return f"No object found with object ID {object_id}."

        image_urls = self.get_object_image_paths(object_id)
        if not image_urls:
            return f"No images are stored for object {object_id}."

        lines = [
            f"Object {object_id} ({obj['category']}): showing {min(len(image_urls), limit)} of {len(image_urls)} frames:",
        ]
        for url in image_urls[:limit]:
            lines.append(f"![frame]({url})")
        return "\n".join(lines)

    def _format_objects_in_image(self, filename: str) -> str:
        objects = self.get_objects_in_image(filename)
        if not objects:
            return f"No objects detected in image '{filename}'."
        count = len(objects)
        lines = [f"Objects detected in image '{filename}' ({count} total):"]
        for obj in objects:
            lines.append(
                f"- Object {obj['object_id']} ({obj['category']}): "
                f"{self._format_xyz(obj['centroid_x'], obj['centroid_y'], obj['centroid_z'], prefix='centroid ')}"
            )
        return "\n".join(lines)

    def _format_category_timeline(self, category: str, inspection_id: int | None = None) -> str:
        objects = self.get_category_timeline(category, inspection_id=inspection_id)
        if not objects:
            return f"No objects found in category '{category}'."

        lines = [f"Timeline for category '{category}' ({len(objects)} object(s)). Object IDs:"]
        for obj in objects:
            first_ns = obj.get("first_seen_ns")
            last_ns = obj.get("last_seen_ns")
            duration_s = ((last_ns - first_ns) / 1e9) if first_ns and last_ns else 0.0
            lines.append(
                f"- Object {obj['id']}: first seen {self._format_timestamp(first_ns)}, "
                f"last seen {self._format_timestamp(last_ns)}, "
                f"{obj['detection_count']} detections over {self._format_duration(duration_s)}"
            )
        lines.append("To see frames, ask: 'show me images of object [ID]'.")
        return "\n".join(lines)

    def _format_category_windows(self, categories: list[str], inspection_id: int | None = None) -> str:
        windows = self.get_category_windows(categories, inspection_id=inspection_id)
        if not windows:
            return f"No data found for categories: {', '.join(categories)}."
        lines = [f"Detection windows for {len(windows)} category/categories:"]
        for row in windows:
            first_ns = row.get("first_seen_ns")
            last_ns = row.get("last_seen_ns")
            lines.append(
                f"- {row['category']}: first seen {self._format_timestamp(first_ns)}, "
                f"last seen {self._format_timestamp(last_ns)}, "
                f"{row['object_count']} distinct object(s)"
            )
        return "\n".join(lines)

    def _format_inspection_timeline(self, inspection_id: int | None = None) -> str:
        objects = self.get_inspection_timeline(inspection_id=inspection_id)
        if not objects:
            return "No objects found in the database."

        first_ns = objects[0].get("first_seen_ns")
        last_ns = objects[-1].get("last_seen_ns")
        duration_s = ((last_ns - first_ns) / 1e9) if first_ns and last_ns else 0.0
        scope = f" (inspection {inspection_id})" if inspection_id is not None else ""

        lines = [
            f"Full inspection timeline{scope}: {len(objects)} objects from {self._format_timestamp(first_ns)} "
            f"to {self._format_timestamp(last_ns)} ({self._format_duration(duration_s)}).",
            "Chronological object log:",
        ]
        for obj in objects[:50]:
            lines.append(
                f"- {self._format_timestamp(obj.get('first_seen_ns'))}: Object {obj['id']} ({obj['category']}), "
                f"{obj['detection_count']} detections"
            )
        if len(objects) > 50:
            lines.append(f"... and {len(objects) - 50} more objects.")
        return "\n".join(lines)

    def _format_temporal_clusters(self, window_ms: int = 500, top_n: int = 10, inspection_id: int | None = None) -> str:
        clusters = self.get_temporal_clusters(window_ms=window_ms, top_n=top_n, inspection_id=inspection_id)
        if not clusters:
            return "No detections found in the database."

        lines = [
            f"Top {len(clusters)} busiest moments (detections grouped within {window_ms} ms):",
        ]
        for idx, cluster in enumerate(clusters, start=1):
            start = self._format_timestamp(cluster["start_ns"])
            end = self._format_timestamp(cluster["end_ns"])
            counts = ", ".join(f"{cnt} x {cat}" for cat, cnt in sorted(cluster["categories"].items(), key=lambda x: -x[1]))
            lines.append(
                f"{idx}. {start} to {end}: {cluster['detection_count']} total detections — {counts}"
            )
        return "\n".join(lines)

    def _format_detection_counts_by_category(self, inspection_id: int | None = None) -> str:
        rows = self.get_detection_counts_by_category(inspection_id=inspection_id)
        if not rows:
            return "No detections found in the database."
        scope = f" (inspection {inspection_id})" if inspection_id is not None else ""
        lines = [f"Per-frame detection counts by category{scope}:"]
        for row in rows:
            lines.append(f"- {row['category']}: {row['count']} detections")
        return "\n".join(lines)

    def _format_objects_in_time_range(
        self, start_time: str | int | float, end_time: str | int | float, limit: int | None = None, inspection_id: int | None = None
    ) -> str:
        objects = self.get_objects_in_time_range(start_time, end_time, limit, inspection_id=inspection_id)
        start_str = self._format_timestamp(self._parse_time_string(start_time))
        end_str = self._format_timestamp(self._parse_time_string(end_time))
        if not objects:
            return f"No objects found between {start_str} and {end_str}."
        lines = [f"{len(objects)} object(s) detected between {start_str} and {end_str}. Object IDs:"]
        for obj in objects:
            lines.append(
                f"- Object {obj['id']} ({obj['category']}): "
                f"{obj['detection_count']} detections, "
                f"{self._format_xyz(obj['centroid_x'], obj['centroid_y'], obj['centroid_z'], prefix='centroid ')}"
            )
        lines.append("To see frames, ask: 'show me images of object [ID]'.")
        return "\n".join(lines)

    def _format_detections_in_time_range(
        self, start_time: str | int | float, end_time: str | int | float, limit: int | None = None, inspection_id: int | None = None
    ) -> str:
        detections = self.get_detections_in_time_range(start_time, end_time, limit, inspection_id=inspection_id)
        start_str = self._format_timestamp(self._parse_time_string(start_time))
        end_str = self._format_timestamp(self._parse_time_string(end_time))
        if not detections:
            return f"No detections found between {start_str} and {end_str}."
        lines = [f"{len(detections)} detection(s) between {start_str} and {end_str}. Object IDs:"]
        for det in detections:
            lines.append(
                f"- {self._format_timestamp(det['timestamp_ns'])}: Object {det['object_id']} ({det['category']}), "
                f"frame {det['filename']}"
            )
        lines.append("To see frames, ask: 'show me images of object [ID]'.")
        return "\n".join(lines)

    def _format_objects_near_position(
        self,
        x: float,
        y: float,
        z: float,
        radius_m: float = 2.0,
        category: str | None = None,
        inspection_id: int | None = None,
    ) -> str:
        objects = self.get_objects_near_position(x, y, z, radius_m, category, inspection_id=inspection_id)
        if not objects:
            cat_str = f" of category '{category}'" if category else ""
            return f"No objects{cat_str} found within {radius_m} m of ({x:.2f}, {y:.2f}, {z:.2f})."
        lines = [
            f"{len(objects)} object(s) within {radius_m} m of ({x:.2f}, {y:.2f}, {z:.2f}). Object IDs:"
        ]
        for obj in objects:
            lines.append(
                f"- Object {obj['id']} ({obj['category']}): "
                f"distance {obj['distance_m']:.2f} m"
            )
        lines.append("To see frames, ask: 'show me images of object [ID]'.")
        return "\n".join(lines)

    def _format_category_sample_images(self, category: str, limit: int | None = None, inspection_id: int | None = None) -> str:
        image_urls = self.get_category_sample_images(category, limit, inspection_id=inspection_id)
        if not image_urls:
            return f"No sample images found for category '{category}'."
        lines = [f"Sample images for category '{category}' ({len(image_urls)} frame(s)). Object IDs visible in each frame are listed below the image:"]
        for url in image_urls:
            filename = Path(url).name
            obj_info = self._image_objects_info(filename)
            lines.append(f"![frame]({url})")
            if obj_info:
                lines.append(f"Objects in this frame: {obj_info}")
        if limit is not None:
            lines.append(
                f"These are a representative subset of the '{category}' frames — "
                "ask if you'd like to see more images."
            )
        lines.append("To see more frames for an object, ask: 'show me images of object [ID]'.")
        return "\n".join(lines)

    def _format_inspection_poses(self, limit: int | None = None, inspection_id: int | None = None) -> str:
        poses = self.get_inspection_poses(limit, inspection_id=inspection_id)
        if not poses:
            return "No inspection poses are recorded yet."
        lines = [f"First {len(poses)} inspection pose(s):"]
        for pose in poses:
            lines.append(
                f"- {pose['filename']} (inspection {pose['inspection_id']}): translation "
                f"({pose['tf_translation_x']:.2f}, {pose['tf_translation_y']:.2f}, {pose['tf_translation_z']:.2f}), "
                f"rotation ({pose['tf_rotation_x']:.2f}, {pose['tf_rotation_y']:.2f}, {pose['tf_rotation_z']:.2f}, {pose['tf_rotation_w']:.2f})"
            )
        return "\n".join(lines)

    def _format_object_distance(self, object_id_a: int, object_id_b: int) -> str:
        result = self.get_object_distance(object_id_a, object_id_b)
        if result is None:
            return f"Could not find both objects {object_id_a} and {object_id_b}."
        return (
            f"Distance between Object {result['object_id_a']} ({result['category_a']}) "
            f"and Object {result['object_id_b']} ({result['category_b']}): "
            f"{result['distance_m']:.2f} m"
        )

    def _format_category_bounding_box(self, category: str, inspection_id: int | None = None) -> str:
        bbox = self.get_category_bounding_box(category, inspection_id=inspection_id)
        if bbox is None:
            return f"No objects found in category '{category}'."
        lines = [
            f"Spatial extent for category '{category}' ({bbox['count']} object(s)):",
        ]
        if any(bbox[k] is None for k in ("min_cx", "max_cx", "min_cy", "max_cy", "min_cz", "max_cz")):
            lines.append("- Centroid range: unknown")
        else:
            lines.append(
                f"- Centroid range: x [{bbox['min_cx']:.2f}, {bbox['max_cx']:.2f}], "
                f"y [{bbox['min_cy']:.2f}, {bbox['max_cy']:.2f}], "
                f"z [{bbox['min_cz']:.2f}, {bbox['max_cz']:.2f}]"
            )
        if any(bbox[k] is None for k in ("min_x", "min_y", "min_z", "max_x", "max_y", "max_z")):
            lines.append("- Bounding box extent: unknown extent")
        else:
            lines.append(
                f"- Bounding box min: ({bbox['min_x']:.2f}, {bbox['min_y']:.2f}, {bbox['min_z']:.2f})"
            )
            lines.append(
                f"- Bounding box max: ({bbox['max_x']:.2f}, {bbox['max_y']:.2f}, {bbox['max_z']:.2f})"
            )
        return "\n".join(lines)

    def _format_category_detection_timeline(
        self, category: str, bucket_seconds: int = 60, inspection_id: int | None = None
    ) -> str:
        rows = self.get_category_detection_timeline(category, bucket_seconds, inspection_id=inspection_id)
        if not rows:
            return f"No detections found for category '{category}'."
        lines = [
            f"Detection timeline for '{category}' ({bucket_seconds}s buckets):"
        ]
        for row in rows:
            lines.append(
                f"- {self._format_timestamp(row['bucket_ns'])}: {row['count']} detections"
            )
        return "\n".join(lines)

    def _format_objects_by_category_in_time_range(
        self,
        category: str,
        start_time: str | int | float,
        end_time: str | int | float,
        limit: int | None = None,
        inspection_id: int | None = None,
    ) -> str:
        objects = self.get_objects_by_category_in_time_range(category, start_time, end_time, limit, inspection_id=inspection_id)
        start_str = self._format_timestamp(self._parse_time_string(start_time))
        end_str = self._format_timestamp(self._parse_time_string(end_time))
        if not objects:
            return f"No {category} objects found between {start_str} and {end_str}."
        lines = [f"{len(objects)} '{category}' object(s) between {start_str} and {end_str}:"]
        for obj in objects:
            lines.append(
                f"- Object {obj['id']}: {obj['detection_count']} detections, "
                f"{self._format_xyz(obj['centroid_x'], obj['centroid_y'], obj['centroid_z'], prefix='centroid ')}"
            )
        return "\n".join(lines)

    def _format_object_movement(self, object_id: int) -> str:
        points = self.get_object_movement(object_id)
        if not points:
            return f"No movement data found for object {object_id}."
        start = points[0]
        end = points[-1]
        dx = end["centroid_x"] - start["centroid_x"]
        dy = end["centroid_y"] - start["centroid_y"]
        dz = end["centroid_z"] - start["centroid_z"]
        displacement = (dx * dx + dy * dy + dz * dz) ** 0.5
        lines = [
            f"Movement path for object {object_id} ({len(points)} detections):",
            f"- Start: {self._format_timestamp(start['timestamp_ns'])} at "
            f"{self._format_xyz(start['centroid_x'], start['centroid_y'], start['centroid_z'])}",
            f"- End:   {self._format_timestamp(end['timestamp_ns'])} at "
            f"{self._format_xyz(end['centroid_x'], end['centroid_y'], end['centroid_z'])}",
            f"- Displacement: {displacement:.2f} m",
        ]
        if len(points) > 2:
            lines.append("Key waypoints:")
            step = max(1, len(points) // 5)
            for i in range(0, len(points), step):
                p = points[i]
                lines.append(
                    f"  - {self._format_timestamp(p['timestamp_ns'])}: "
                    f"{self._format_xyz(p['centroid_x'], p['centroid_y'], p['centroid_z'])}"
                )
        return "\n".join(lines)

    def _format_nearest_objects_to_object(self, object_id: int, radius_m: float = 2.0, inspection_id: int | None = None) -> str:
        results = self.get_nearest_objects_to_object(object_id, radius_m, inspection_id=inspection_id)
        if not results:
            return f"No objects found within {radius_m} m of object {object_id}."
        lines = [f"Objects within {radius_m} m of object {object_id}:"]
        for r in results:
            lines.append(
                f"- Object {r['id']} ({r['category']}): "
                f"{r['distance_m']:.2f} m away at "
                f"{self._format_xyz(r['centroid_x'], r['centroid_y'], r['centroid_z'])}"
            )
        return "\n".join(lines)

    def _format_images_in_time_range(
        self,
        start_time: str | int | float,
        end_time: str | int | float,
        category: str | None = None,
        limit: int | None = None,
        inspection_id: int | None = None,
    ) -> str:
        total_images = self._count_images_in_time_range(start_time, end_time, category, inspection_id=inspection_id)
        image_urls = self.get_images_in_time_range(start_time, end_time, category, limit, inspection_id=inspection_id)
        start_str = self._format_timestamp(self._parse_time_string(start_time))
        end_str = self._format_timestamp(self._parse_time_string(end_time))
        if not image_urls:
            cat_str = f" of category '{category}'" if category else ""
            return f"No images{cat_str} found between {start_str} and {end_str}."
        cat_str = f" ({category})" if category else ""
        lines = [
            f"{total_images} distinct images were captured between {start_str} and {end_str}{cat_str}. "
            f"Showing {len(image_urls)} frames. Object IDs visible in each frame are listed below the image:"
        ]
        for url in image_urls:
            filename = Path(url).name
            obj_info = self._image_objects_info(filename)
            lines.append(f"![frame]({url})")
            if obj_info:
                lines.append(f"Objects in this frame: {obj_info}")
        return "\n".join(lines)

    def _count_images_in_time_range(
        self,
        start_time: str | int | float,
        end_time: str | int | float,
        category: str | None = None,
        inspection_id: int | None = None,
    ) -> int:
        start_ns = self._parse_time_string(start_time)
        end_ns = self._parse_time_string(end_time)
        if start_ns is None or end_ns is None:
            return 0
        sql = """
            SELECT COUNT(DISTINCT i.filename) AS c
            FROM images i
            WHERE i.timestamp_ns >= ? AND i.timestamp_ns <= ? AND i.filename IS NOT NULL
        """
        params: list[Any] = [start_ns, end_ns]
        if category:
            sql += " AND i.id IN (SELECT d.image_id FROM detections d JOIN objects o ON o.id=d.object_id JOIN categories c ON c.id=o.category_id WHERE c.name=?)"
            params.append(category)
        if inspection_id is not None:
            sql += " AND i.inspection_id=?"
            params.append(inspection_id)
        row = self._connect().execute(sql, tuple(params)).fetchone()
        return int(row["c"] or 0) if row else 0

    def _format_category_cooccurrence(
        self, window_ms: int = 500, top_n: int = 10, inspection_id: int | None = None
    ) -> str:
        pairs = self.get_category_cooccurrence(window_ms, top_n, inspection_id=inspection_id)
        if not pairs:
            return "No category co-occurrence data found."
        lines = [f"Top {len(pairs)} category pairs seen together (within {window_ms} ms clusters):"]
        for p in pairs:
            lines.append(f"- {p['category_a']} + {p['category_b']}: {p['cluster_count']} clusters")
        return "\n".join(lines)

    def _format_objects_in_temporal_cluster(
        self,
        center_time: str | int | float,
        window_ms: int = 500,
        limit: int | None = None,
        inspection_id: int | None = None,
    ) -> str:
        data = self.get_objects_in_temporal_cluster(center_time, window_ms, limit, inspection_id=inspection_id)
        start_str = self._format_timestamp(data["start_time_ns"])
        end_str = self._format_timestamp(data["end_time_ns"])
        if not data["objects"] and not data["detections"]:
            return f"No objects detected around {self._format_timestamp(data['center_time_ns'])}."
        lines = [
            f"Objects detected around {self._format_timestamp(data['center_time_ns'])} "
            f"({start_str} to {end_str}):",
            "Detection counts by category:",
        ]
        for cat, cnt in sorted(data["category_counts"].items(), key=lambda x: -x[1]):
            lines.append(f"- {cat}: {cnt}")
        lines.append("Objects with coordinates:")
        for obj in data["objects"][:20]:
            lines.append(
                f"- Object {obj['id']} ({obj['category']}): "
                f"{self._format_xyz(obj['centroid_x'], obj['centroid_y'], obj['centroid_z'], prefix='centroid ')}"
                f", {obj['detection_count']} detections"
            )
        if len(data["objects"]) > 20:
            lines.append(f"... and {len(data['objects']) - 20} more objects.")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Anomaly formatters
    # ------------------------------------------------------------------

    def _format_anomaly_types(self) -> str:
        if not self._anomaly_tables_exist():
            return "Anomaly tables (anomaly_types, abnormal_detections, abnormalities) are not yet populated in this database."
        types = self.get_anomaly_types()
        if not types:
            return "No anomaly types are defined yet."
        return "Known anomaly types:\n" + "\n".join(f"- {t}" for t in types)

    def _format_anomaly_summary(self, inspection_id: int | None = None) -> str:
        if not self._anomaly_tables_exist():
            return "Anomaly tables (anomaly_types, abnormal_detections, abnormalities) are not yet populated in this database."
        data = self.get_anomaly_summary(inspection_id=inspection_id)
        scope = f" (inspection {inspection_id})" if inspection_id is not None else ""
        lines = [
            f"Anomaly summary{scope}: {data['total_abnormalities']} abnormalit(ies) across {data['total_pairs']} image pair(s)."
        ]
        if data["by_type"]:
            lines.append("By type:")
            for row in data["by_type"]:
                lines.append(f"- {row['type']}: {row['count']}")
        if data["by_inspection"]:
            lines.append("By inspection:")
            for row in data["by_inspection"]:
                lines.append(f"- Inspection {row['inspection_id']}: {row['count']}")
        lines.append("Use get_anomalies for the individual abnormalities with their image pairs.")
        return "\n".join(lines)

    def _format_anomalies(
        self,
        anomaly_id: int | None = None,
        anomaly_type: str | None = None,
        inspection_id: int | None = None,
        limit: int | None = None,
    ) -> str:
        if not self._anomaly_tables_exist():
            return "Anomaly tables (anomaly_types, abnormal_detections, abnormalities) are not yet populated in this database."
        rows = self.get_anomalies(anomaly_id=anomaly_id, anomaly_type=anomaly_type, inspection_id=inspection_id, limit=limit)
        if not rows:
            return "No abnormalities found matching the filter."
        lines = [f"{len(rows)} abnormalit(ies):"]
        for r in rows:
            bbox = (
                f"bbox ({r['min_x']}, {r['min_y']})-({r['max_x']}, {r['max_y']})"
                if r["min_x"] is not None else "no bbox"
            )
            lines.append(
                f"- Anomaly {r['id']} (inspection {r['inspection_id']}): type='{r['type']}', {bbox}"
            )
            if r.get("object"):
                lines.append(f"  object: {r['object']}")
            if r.get("location"):
                lines.append(f"  location in scene: {r['location']}")
            if r.get("note"):
                lines.append(f"  note: {r['note']}")
            if r.get("pair_summary"):
                lines.append(f"  pair summary: {r['pair_summary']}")
            if r.get("cam_x") is not None:
                lines.append(
                    f"  observed from camera position {self._format_xyz(r['cam_x'], r['cam_y'], r['cam_z'])} "
                    f"(marked in the Rerun viewer)"
                )
            if r.get("inspection_image_url"):
                lines.append(f"  inspection frame: ![inspection frame]({r['inspection_image_url']})")
            if r.get("gt_image_url"):
                lines.append(f"  ground-truth frame: ![gt frame]({r['gt_image_url']})")
        return "\n".join(lines)

    def _format_anomaly_locations(self, inspection_id: int | None = None) -> str:
        if not self._anomaly_tables_exist():
            return "Anomaly tables (anomaly_types, abnormal_detections, abnormalities) are not yet populated in this database."
        rows = self.get_anomaly_locations(inspection_id=inspection_id)
        if not rows:
            return "No anomaly locations found."
        scope = f" (inspection {inspection_id})" if inspection_id is not None else ""
        lines = [
            f"{len(rows)} anomaly location(s){scope} — camera positions where the abnormal frames were taken "
            f"(these coordinates are marked in the Rerun viewer). "
            f"Each line is numbered by the individual abnormality id (not the internal image-pair id):"
        ]
        for r in rows:
            pos = self._format_xyz(r["cam_x"], r["cam_y"], r["cam_z"])
            if r["cam_x"] is None:
                pos = f"{self._format_xyz(r['gt_x'], r['gt_y'], r['gt_z'])} (ground-truth viewpoint)"
            obj = r.get("object") or "unknown"
            lines.append(
                f"- Anomaly {r['abnormality_id']} [inspection {r['inspection_id']}]: "
                f"{r.get('type') or 'unknown'} {obj} at {pos}"
            )
        return "\n".join(lines)

    def _format_sql_query_result(self, query: str, limit: int = 100) -> str:
        result = self.run_sql_query(query, limit)
        if "error" in result:
            return f"SQL query failed: {result['error']}\nQuery: {result['query']}"

        timestamp_cols = {"first_seen_ns", "last_seen_ns", "timestamp_ns", "bucket_ns", "started_at"}

        def _format_row_value(key: str, value: Any) -> Any:
            if key in timestamp_cols and isinstance(value, (int, float)) and value > 1e12:
                return self._format_timestamp(int(value))
            return value

        rows = [{k: _format_row_value(k, v) for k, v in row.items()} for row in result["rows"]]

        lines = [
            f"SQL query returned {result['row_count']} row(s):",
            f"Query: {result['query']}",
        ]
        if result["columns"]:
            lines.append("Columns: " + ", ".join(result["columns"]))
        for row in rows:
            lines.append(str(row))
        return "\n".join(lines)