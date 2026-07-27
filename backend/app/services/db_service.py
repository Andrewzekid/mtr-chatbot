from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from app.services.tool_router import ToolRouter

logger = logging.getLogger(__name__)


class InspectionDBClient:
    """SQLite client for the MTR inspection object database.

    Provides structured queries and a simple keyword-based natural language
    lookup so the voice assistant can answer questions about detected objects.
    """

    # Category aliases map colloquial terms to the category strings stored in DB.
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
        # Exact case-insensitive match against DB values.
        for alias, canonical in cls._CATEGORY_ALIASES.items():
            if canonical.lower() == key:
                return canonical
        return name.strip()

    def __init__(self, db_path: str | Path, router: ToolRouter | None = None) -> None:
        self.db_path = Path(db_path)
        self.router = router
        self._conn: sqlite3.Connection | None = None
        self._last_tool_calls: list[dict[str, Any]] = []
        self._last_tool_results: list[dict[str, Any]] = []

    @staticmethod
    def _format_timestamp(ns: int | None) -> str:
        if ns is None:
            return "unknown"
        try:
            dt = datetime.fromtimestamp(ns / 1e9)
            return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
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
        if not image_path:
            return None
        name = Path(image_path).name
        if not name:
            return None
        return f"/inspection/images/{name}"

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
    # Structured queries
    # ------------------------------------------------------------------

    def get_summary(self) -> dict[str, Any]:
        """Overall counts and category breakdown."""
        conn = self._connect()
        total_objects = conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
        categories = conn.execute(
            "SELECT category, COUNT(*) AS count FROM objects GROUP BY category ORDER BY count DESC"
        ).fetchall()
        return {
            "total_objects": total_objects,
            "categories": [{"category": row["category"], "count": row["count"]} for row in categories],
        }

    def get_categories(self) -> list[str]:
        """Return the distinct category names present in the objects table."""
        conn = self._connect()
        rows = conn.execute(
            "SELECT DISTINCT category FROM objects WHERE category IS NOT NULL ORDER BY category"
        ).fetchall()
        return [row["category"] for row in rows]

    def query_database(self, sql_query: str, limit: int = 100) -> dict[str, Any]:
        """Execute a read-only SELECT query and return the results."""
        return self.run_sql_query(sql_query, limit)

    def get_objects_by_category(self, category: str, limit: int = 20) -> list[dict[str, Any]]:
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT track_id, category, observation_count, total_point_count,
                   centroid_x, centroid_y, centroid_z,
                   bbox3d_min_x, bbox3d_min_y, bbox3d_min_z,
                   bbox3d_max_x, bbox3d_max_y, bbox3d_max_z,
                   first_seen_ns, last_seen_ns
            FROM objects
            WHERE category = ?
            ORDER BY total_point_count DESC
            LIMIT ?
            """,
            (category, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_category_objects_with_coordinates(self, category: str) -> list[dict[str, Any]]:
        """Return every object in a category with centroid and bounding-box coordinates."""
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT track_id, category, observation_count, total_point_count,
                   centroid_x, centroid_y, centroid_z,
                   bbox3d_min_x, bbox3d_min_y, bbox3d_min_z,
                   bbox3d_max_x, bbox3d_max_y, bbox3d_max_z,
                   first_seen_ns, last_seen_ns
            FROM objects
            WHERE category = ?
            ORDER BY first_seen_ns
            """,
            (category,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_category_proximity(
        self,
        target_category: str,
        other_categories: list[str],
        radius_m: float = 2.0,
    ) -> list[dict[str, Any]]:
        """For each object in *target_category*, count how many objects from
        *other_categories* have centroids within *radius_m* meters.
        """
        targets = self.get_category_objects_with_coordinates(target_category)
        if not targets or not other_categories:
            return []

        conn = self._connect()
        placeholders = ",".join("?" for _ in other_categories)
        others = conn.execute(
            f"""
            SELECT track_id, category, centroid_x, centroid_y, centroid_z
            FROM objects
            WHERE category IN ({placeholders})
            """,
            other_categories,
        ).fetchall()

        results = []
        for target in targets:
            tx, ty, tz = target["centroid_x"], target["centroid_y"], target["centroid_z"]
            nearby: dict[str, int] = {}
            for row in others:
                if row["track_id"] == target["track_id"]:
                    continue
                dx = row["centroid_x"] - tx
                dy = row["centroid_y"] - ty
                dz = row["centroid_z"] - tz
                dist = (dx * dx + dy * dy + dz * dz) ** 0.5
                if dist <= radius_m:
                    cat = row["category"]
                    nearby[cat] = nearby.get(cat, 0) + 1
            results.append(
                {
                    "track_id": target["track_id"],
                    "centroid_x": tx,
                    "centroid_y": ty,
                    "centroid_z": tz,
                    "nearby": nearby,
                }
            )
        return results

    def get_object_by_track_id(self, track_id: int) -> dict[str, Any] | None:
        conn = self._connect()
        row = conn.execute(
            """
            SELECT track_id, category, observation_count, total_point_count,
                   centroid_x, centroid_y, centroid_z,
                   bbox3d_min_x, bbox3d_min_y, bbox3d_min_z,
                   bbox3d_max_x, bbox3d_max_y, bbox3d_max_z,
                   first_seen_ns, last_seen_ns, aggregated_pcd_path
            FROM objects
            WHERE track_id = ?
            """,
            (track_id,),
        ).fetchone()
        if row is None:
            return None
        obj = dict(row)
        obj["observations"] = conn.execute(
            """
            SELECT timestamp_ns, image_file_name, point_count,
                   centroid_x, centroid_y, centroid_z,
                   pcd_path, mask_path, image_path
            FROM observations
            WHERE track_id = ?
            ORDER BY timestamp_ns
            """,
            (track_id,),
        ).fetchall()
        return obj

    def get_top_objects(self, n: int = 5) -> list[dict[str, Any]]:
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT track_id, category, observation_count, total_point_count,
                   centroid_x, centroid_y, centroid_z
            FROM objects
            ORDER BY total_point_count DESC
            LIMIT ?
            """,
            (n,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_recent_objects(self, limit: int = 5) -> list[dict[str, Any]]:
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT track_id, category, observation_count, total_point_count,
                   centroid_x, centroid_y, centroid_z, last_seen_ns
            FROM objects
            ORDER BY last_seen_ns DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_object_timeline(self, track_id: int) -> list[dict[str, Any]]:
        """Return every observation for a track, ordered by timestamp."""
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT timestamp_ns, image_file_name, image_path, point_count,
                   centroid_x, centroid_y, centroid_z
            FROM observations
            WHERE track_id = ?
            ORDER BY timestamp_ns
            """,
            (track_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_object_image_paths(self, track_id: int) -> list[str]:
        """Return distinct image paths for a track."""
        conn = self._connect()
        rows = conn.execute(
            "SELECT DISTINCT image_path FROM observations WHERE track_id = ? AND image_path IS NOT NULL",
            (track_id,),
        ).fetchall()
        return [row["image_path"] for row in rows if row["image_path"]]

    def get_category_timeline(self, category: str) -> list[dict[str, Any]]:
        """Return first/last seen timestamps for every object in a category."""
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT track_id, category, observation_count, total_point_count,
                   first_seen_ns, last_seen_ns
            FROM objects
            WHERE category = ?
            ORDER BY first_seen_ns
            """,
            (category,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_inspection_timeline(self) -> list[dict[str, Any]]:
        """Return first/last seen timestamps for every object, ordered by first_seen."""
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT track_id, category, observation_count, total_point_count,
                   first_seen_ns, last_seen_ns
            FROM objects
            ORDER BY first_seen_ns
            """,
        ).fetchall()
        return [dict(row) for row in rows]

    def get_category_windows(self, categories: list[str]) -> list[dict[str, Any]]:
        """Return first/last detection windows for one or more categories."""
        if not categories:
            return []
        placeholders = ",".join("?" for _ in categories)
        conn = self._connect()
        rows = conn.execute(
            f"""
            SELECT category,
                   MIN(first_seen_ns) AS first_seen_ns,
                   MAX(last_seen_ns) AS last_seen_ns,
                   COUNT(*) AS object_count
            FROM objects
            WHERE category IN ({placeholders})
            GROUP BY category
            ORDER BY first_seen_ns
            """,
            categories,
        ).fetchall()
        return [dict(row) for row in rows]

    def get_temporal_clusters(self, window_ms: int = 500, top_n: int = 20) -> list[dict[str, Any]]:
        """Group objects into time-window clusters and count categories in each.

        A cluster is a consecutive run of object first-seen timestamps that are
        within *window_ms* of each other. This reveals which kinds of objects were
        seen together at a given moment. Uses the `objects` table, not `observations`.
        """
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT first_seen_ns, category
            FROM objects
            WHERE category IS NOT NULL AND first_seen_ns IS NOT NULL
            ORDER BY first_seen_ns
            """
        ).fetchall()

        if not rows:
            return []

        window_ns = window_ms * 1_000_000
        clusters: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None

        for row in rows:
            ts = row["first_seen_ns"]
            category = row["category"]
            if current is None or ts - current["end_ns"] > window_ns:
                if current is not None:
                    clusters.append(current)
                current = {
                    "start_ns": ts,
                    "end_ns": ts,
                    "categories": {},
                    "object_count": 0,
                }
            current["end_ns"] = ts
            current["categories"][category] = current["categories"].get(category, 0) + 1
            current["object_count"] += 1

        if current is not None:
            clusters.append(current)

        # Sort by total object count so the busiest moments come first.
        clusters.sort(key=lambda c: c["object_count"], reverse=True)
        return clusters[:top_n]

    # ------------------------------------------------------------------
    # Additional spatial / temporal / audit helpers
    # ------------------------------------------------------------------

    def _get_inspection_base_date(self) -> datetime.date:
        """Return the date of the earliest observation, or today if none."""
        conn = self._connect()
        row = conn.execute("SELECT MIN(timestamp_ns) FROM observations").fetchone()
        if row and row[0]:
            return datetime.fromtimestamp(row[0] / 1e9).date()
        return datetime.now().date()

    def _get_inspection_time_range_ns(self) -> tuple[int, int]:
        """Return (min_timestamp_ns, max_timestamp_ns) from observations."""
        conn = self._connect()
        row = conn.execute(
            "SELECT MIN(timestamp_ns), MAX(timestamp_ns) FROM observations"
        ).fetchone()
        if row and row[0] and row[1]:
            return int(row[0]), int(row[1])
        return 0, 0

    def _parse_time_string(self, value: str | int | float) -> int | None:
        """Convert a time string or integer into nanoseconds since epoch.

        Supports:
        - integer/float nanoseconds
        - ISO datetime strings
        - clock-only strings (e.g. '16:51:45', '4:51 PM'), interpreted on the
          inspection date
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
                # If the bare time falls outside the recorded inspection window,
                # try shifting by 12 hours to handle colloquial AM/PM omission.
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

    def get_observation_counts_by_category(self) -> list[dict[str, Any]]:
        """Per-frame observation counts by category."""
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT category, COUNT(*) AS count
            FROM observations
            WHERE category IS NOT NULL
            GROUP BY category
            ORDER BY count DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def get_objects_in_time_range(
        self, start_time: str | int | float, end_time: str | int | float, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Objects whose detection span overlaps [start, end]."""
        start_ns = self._parse_time_string(start_time)
        end_ns = self._parse_time_string(end_time)
        if start_ns is None or end_ns is None:
            return []
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT track_id, category, observation_count, total_point_count,
                   centroid_x, centroid_y, centroid_z,
                   first_seen_ns, last_seen_ns
            FROM objects
            WHERE first_seen_ns <= ? AND last_seen_ns >= ?
            ORDER BY first_seen_ns
            LIMIT ?
            """,
            (end_ns, start_ns, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_observations_in_time_range(
        self, start_time: str | int | float, end_time: str | int | float, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Per-frame observations captured within [start, end]."""
        start_ns = self._parse_time_string(start_time)
        end_ns = self._parse_time_string(end_time)
        if start_ns is None or end_ns is None:
            return []
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT timestamp_ns, track_id, category, image_file_name, point_count,
                   centroid_x, centroid_y, centroid_z, image_path
            FROM observations
            WHERE timestamp_ns >= ? AND timestamp_ns <= ?
            ORDER BY timestamp_ns
            LIMIT ?
            """,
            (start_ns, end_ns, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_objects_near_position(
        self,
        x: float,
        y: float,
        z: float,
        radius_m: float = 2.0,
        category: str | None = None,
    ) -> list[dict[str, Any]]:
        """Objects whose centroid is within radius_m of (x, y, z)."""
        conn = self._connect()
        sql = "SELECT track_id, category, centroid_x, centroid_y, centroid_z FROM objects"
        params: list[Any] = []
        if category:
            sql += " WHERE category = ?"
            params.append(category)
        rows = conn.execute(sql, params).fetchall()

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

    def get_category_sample_images(self, category: str, limit: int = 5) -> list[str]:
        """Distinct image paths for a category, sampled randomly."""
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT DISTINCT image_path
            FROM observations
            WHERE category = ? AND image_path IS NOT NULL
            ORDER BY RANDOM()
            LIMIT ?
            """,
            (category, limit),
        ).fetchall()
        return [row["image_path"] for row in rows if row["image_path"]]

    def get_inspection_poses(self, limit: int = 20) -> list[dict[str, Any]]:
        """Camera/robot poses from the inspection_poses table."""
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT image_path,
                   tf_translation_x, tf_translation_y, tf_translation_z,
                   tf_rotation_x, tf_rotation_y, tf_rotation_z, tf_rotation_w
            FROM inspection_poses
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_filtered_objects(self, limit: int = 50) -> list[dict[str, Any]]:
        """Tracks/objects dropped by the merge/filter layer."""
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT track_id, category, reason, point_count,
                   bbox3d_min_x, bbox3d_min_y, bbox3d_min_z,
                   bbox3d_max_x, bbox3d_max_y, bbox3d_max_z,
                   first_seen_ns, last_seen_ns
            FROM filtered_objects
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_object_distance(self, track_id_a: int, track_id_b: int) -> dict[str, Any] | None:
        """Distance between the centroids of two tracks."""
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT track_id, category, centroid_x, centroid_y, centroid_z
            FROM objects
            WHERE track_id IN (?, ?)
            """,
            (track_id_a, track_id_b),
        ).fetchall()
        if len(rows) != 2:
            return None
        a, b = rows[0], rows[1]
        dx = a["centroid_x"] - b["centroid_x"]
        dy = a["centroid_y"] - b["centroid_y"]
        dz = a["centroid_z"] - b["centroid_z"]
        return {
            "track_id_a": a["track_id"],
            "category_a": a["category"],
            "track_id_b": b["track_id"],
            "category_b": b["category"],
            "distance_m": round((dx * dx + dy * dy + dz * dz) ** 0.5, 3),
        }

    def get_category_bounding_box(self, category: str) -> dict[str, Any] | None:
        """Axis-aligned 3D bounding box of all objects in a category."""
        conn = self._connect()
        row = conn.execute(
            """
            SELECT COUNT(*) AS count,
                   MIN(centroid_x) AS min_cx, MAX(centroid_x) AS max_cx,
                   MIN(centroid_y) AS min_cy, MAX(centroid_y) AS max_cy,
                   MIN(centroid_z) AS min_cz, MAX(centroid_z) AS max_cz,
                   MIN(bbox3d_min_x) AS min_x, MAX(bbox3d_max_x) AS max_x,
                   MIN(bbox3d_min_y) AS min_y, MAX(bbox3d_max_y) AS max_y,
                   MIN(bbox3d_min_z) AS min_z, MAX(bbox3d_max_z) AS max_z
            FROM objects
            WHERE category = ?
            """,
            (category,),
        ).fetchone()
        if row is None or row["count"] == 0:
            return None
        return dict(row)

    def get_category_observation_timeline(
        self, category: str, bucket_seconds: int = 60
    ) -> list[dict[str, Any]]:
        """Per-time-bucket observation counts for a category."""
        conn = self._connect()
        bucket_ns = int(bucket_seconds * 1e9)
        rows = conn.execute(
            """
            SELECT (timestamp_ns / ?) * ? AS bucket_ns, COUNT(*) AS count
            FROM observations
            WHERE category = ? AND timestamp_ns IS NOT NULL
            GROUP BY bucket_ns
            ORDER BY bucket_ns
            """,
            (bucket_ns, bucket_ns, category),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_objects_by_category_in_time_range(
        self,
        category: str,
        start_time: str | int | float,
        end_time: str | int | float,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Objects of a category whose detection span overlaps a time window."""
        start_ns = self._parse_time_string(start_time)
        end_ns = self._parse_time_string(end_time)
        if start_ns is None or end_ns is None:
            return []
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT track_id, category, observation_count, total_point_count,
                   centroid_x, centroid_y, centroid_z,
                   first_seen_ns, last_seen_ns
            FROM objects
            WHERE category = ? AND first_seen_ns <= ? AND last_seen_ns >= ?
            ORDER BY first_seen_ns
            LIMIT ?
            """,
            (category, end_ns, start_ns, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_object_movement(self, track_id: int) -> list[dict[str, Any]]:
        """Centroid path of a track across its observations."""
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT timestamp_ns, centroid_x, centroid_y, centroid_z, point_count, image_path
            FROM observations
            WHERE track_id = ?
            ORDER BY timestamp_ns
            """,
            (track_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_nearest_objects_to_track(
        self, track_id: int, radius_m: float = 2.0
    ) -> list[dict[str, Any]]:
        """Other objects within radius_m of a track's centroid."""
        conn = self._connect()
        target = conn.execute(
            "SELECT category, centroid_x, centroid_y, centroid_z FROM objects WHERE track_id = ?",
            (track_id,),
        ).fetchone()
        if target is None:
            return []
        tx, ty, tz = target["centroid_x"], target["centroid_y"], target["centroid_z"]
        rows = conn.execute(
            "SELECT track_id, category, centroid_x, centroid_y, centroid_z FROM objects WHERE track_id != ?",
            (track_id,),
        ).fetchall()
        results = []
        for row in rows:
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
        limit: int = 5,
    ) -> list[str]:
        """Distinct image paths captured in a time window, optionally filtered by category."""
        start_ns = self._parse_time_string(start_time)
        end_ns = self._parse_time_string(end_time)
        if start_ns is None or end_ns is None:
            return []
        conn = self._connect()
        sql = """
            SELECT DISTINCT image_path
            FROM observations
            WHERE timestamp_ns >= ? AND timestamp_ns <= ? AND image_path IS NOT NULL
        """
        params: list[Any] = [start_ns, end_ns]
        if category:
            sql += " AND category = ?"
            params.append(category)
        sql += " ORDER BY RANDOM() LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [row["image_path"] for row in rows if row["image_path"]]

    def get_category_cooccurrence(
        self, window_ms: int = 500, top_n: int = 10
    ) -> list[dict[str, Any]]:
        """Count how often pairs of categories appear in the same temporal cluster."""
        clusters = self.get_temporal_clusters(window_ms=window_ms, top_n=10000)
        pair_counts: dict[tuple[str, str], int] = {}
        for cluster in clusters:
            cats = sorted(cluster["categories"].keys())
            for i, a in enumerate(cats):
                for b in cats[i + 1 :]:
                    key = (a, b)
                    pair_counts[key] = pair_counts.get(key, 0) + 1
        sorted_pairs = sorted(pair_counts.items(), key=lambda item: -item[1])[:top_n]
        return [{"category_a": a, "category_b": b, "cluster_count": count} for (a, b), count in sorted_pairs]

    def get_objects_in_temporal_cluster(
        self,
        center_time: str | int | float,
        window_ms: int = 500,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Objects with coordinates detected around a specific time."""
        center_ns = self._parse_time_string(center_time)
        if center_ns is None:
            return {"center_time": center_time, "objects": [], "observations": [], "category_counts": {}}
        window_ns = window_ms * 1_000_000
        start_ns = center_ns - window_ns // 2
        end_ns = center_ns + window_ns // 2
        conn = self._connect()
        obs_rows = conn.execute(
            """
            SELECT timestamp_ns, track_id, category, centroid_x, centroid_y, centroid_z, image_path
            FROM observations
            WHERE timestamp_ns >= ? AND timestamp_ns <= ?
            ORDER BY timestamp_ns
            LIMIT ?
            """,
            (start_ns, end_ns, limit),
        ).fetchall()
        observations = [dict(row) for row in obs_rows]

        obj_rows = conn.execute(
            """
            SELECT track_id, category, centroid_x, centroid_y, centroid_z,
                   first_seen_ns, last_seen_ns, observation_count
            FROM objects
            WHERE first_seen_ns <= ? AND last_seen_ns >= ?
            ORDER BY first_seen_ns
            LIMIT ?
            """,
            (end_ns, start_ns, limit),
        ).fetchall()
        objects = [dict(row) for row in obj_rows]

        category_counts: dict[str, int] = {}
        for obs in observations:
            cat = obs.get("category")
            if cat:
                category_counts[cat] = category_counts.get(cat, 0) + 1

        return {
            "center_time_ns": center_ns,
            "window_ms": window_ms,
            "start_time_ns": start_ns,
            "end_time_ns": end_ns,
            "category_counts": category_counts,
            "objects": objects,
            "observations": observations,
        }

    def run_sql_query(self, query: str, limit: int = 100) -> dict[str, Any]:
        """Execute a read-only SELECT query and return the results."""
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
            rows = conn.execute(cleaned).fetchmany(limit)
            columns = [desc[0] for desc in conn.execute(cleaned).description] if rows else []
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

    def lookup(self, query: str) -> str | None:
        """Return a text summary for DB-related queries, or None if unrelated."""
        q = query.lower().strip()
        if not q:
            return None

        # Bail early if the query is clearly not about the inspection database.
        db_keywords = [
            "object", "objects", "track", "tracks", "observation", "observations",
            "category", "categories", "point", "points", "poster", "advertisement",
            "exit sign", "light", "lights", "map", "tv", "ticket gate", "gate",
            "database", "db", "inspect", "inspection", "found", "detected",
            "largest", "biggest", "smallest", "recent", "last seen", "summary",
            "count", "how many", "what did", "what is", "where is", "tell me about",
            "when", "timeline", "story", "history", "duration", "first seen", "last seen", "seen",
            "timestamp", "timestamps", "time", "times",
            "image", "images", "picture", "pictures", "photo", "photos", "show me",
            "look at", "visual", "see", "frame", "frames",
            "cluster", "clusters", "group", "groups", "grouped", "together", "consecutive",
            "consecutively", "snapshot", "snapshots", "moment", "moments", "same time",
            "at time", "around time", "busy", "busiest", "busiest minute", "activity",
            "coordinate", "coordinates", "position", "positions", "proximity", "near",
            "nearby", "close", "close to", "next to", "adjacent",
            "between", "around", "happened", "detections", "range", "period", "window",
            "sample", "example", "examples",
            "distance", "far", "apart",
            "area", "extent", "occupy", "bounding box", "bounds",
            "filtered", "dropped", "removed",
            "pose", "poses", "trajectory", "camera pose", "robot pose",
            "movement", "moved", "move", "path", "waypoints", "displacement",
            "location", "located", "where was", "where is", "where were",
            "sql", "query", "run a query", "select",
            "find", "anomaly", "anomalies",
        ]
        if not any(kw in q for kw in db_keywords):
            return None

        # Try LLM-based tool selection first.
        if self.router is not None:
            try:
                tool_calls = self.router.select_tool(query)
                self._record_tool_calls(tool_calls)
                if tool_calls:
                    results: list[str] = []
                    tool_results: list[dict[str, Any]] = []
                    for tool_name, args in tool_calls:
                        result = self._execute_tool(tool_name, args)
                        tool_results.append({"name": tool_name, "args": args, "output": result})
                        if result:
                            results.append(result)
                    self._record_tool_results(tool_results)
                    if results:
                        return "\n\n".join(results)
                else:
                    self._record_tool_results([])
            except Exception as exc:
                logger.warning("LLM router execution failed: %s", exc)

        image_keywords = ("image", "images", "picture", "pictures", "photo", "photos", "show me", "look at", "visual", "frame", "frames")
        temporal_keywords = ("when", "timeline", "story", "history", "duration", "first seen", "last seen", "seen", "timestamp", "timestamps", "time", "times")

        try:
            # Specific track / object ID
            track_match = re.search(r"(?:track|object|id)\s*#?\s*(\d+)", q)
            if track_match:
                track_id = int(track_match.group(1))
                if any(kw in q for kw in image_keywords):
                    return self._format_object_images(track_id)
                if any(kw in q for kw in temporal_keywords):
                    return self._format_object_timeline(track_id)
                return self._format_object(track_id)

            # Top / largest objects
            if any(kw in q for kw in ("largest", "biggest", "most points", "top")):
                return self._format_top_objects()

            # Recent / last seen
            if any(kw in q for kw in ("recent", "last seen", "latest")):
                return self._format_recent_objects()

            # Temporal inspection story
            if any(kw in q for kw in temporal_keywords):
                # Category-specific timeline if a category alias is present.
                for alias, canonical in self._CATEGORY_ALIASES.items():
                    if alias in q:
                        return self._format_category_timeline(canonical)
                return self._format_inspection_timeline()

            # Coordinates and/or proximity for categories
            wants_coordinates = any(kw in q for kw in ("coordinate", "coordinates", "position", "positions", "x y z", "xyz"))
            wants_proximity = any(kw in q for kw in ("near", "nearby", "close", "close to", "next to", "proximity", "adjacent"))
            if wants_coordinates or wants_proximity:
                # Preserve earliest appearance order and deduplicate canonical categories.
                seen: dict[str, int] = {}
                for alias, canonical in self._CATEGORY_ALIASES.items():
                    if alias in q and canonical not in seen:
                        seen[canonical] = q.find(alias)
                matched = sorted(seen.items(), key=lambda item: item[1])
                if matched:
                    target = matched[0][0]
                    results: list[str] = []
                    if wants_coordinates:
                        results.append(self._format_category_coordinates(target))
                    if wants_proximity and len(matched) >= 2:
                        others = [canon for canon, _ in matched[1:]]
                        results.append(self._format_category_proximity(target, others, radius_m=2.0))
                    if results:
                        return "\n\n".join(results)

            # Time-window clusters: objects seen together / consecutively
            cluster_keywords = ("cluster", "clusters", "group", "groups", "grouped", "together", "consecutive", "consecutively", "snapshot", "snapshots", "moment", "moments", "same time", "at time", "around time", "busy", "busiest")
            if any(kw in q for kw in cluster_keywords):
                return self._format_temporal_clusters()

            # Category lookup
            for alias, canonical in self._CATEGORY_ALIASES.items():
                if alias in q:
                    return self._format_category(canonical)

            # Generic summary / count
            if any(kw in q for kw in ("summary", "overview", "how many", "count", "what did", "what do you see", "what did you find")):
                return self._format_summary()

            # Fallback: if "object" or "objects" appears, give summary.
            if "object" in q or "objects" in q:
                return self._format_summary()

        except sqlite3.Error as exc:
            logger.warning("Inspection DB query failed: %s", exc)
            return None

        return None

    def _execute_tool(self, tool_name: str, args: dict[str, Any]) -> str | None:
        """Execute a tool selected by the LLM router and return its formatted result."""
        try:
            if tool_name == "get_summary":
                return self._format_summary()
            if tool_name == "get_categories":
                return self._format_categories()
            if tool_name == "query_database":
                return self._format_query_database(args["sql_query"], limit=int(args.get("limit", 100)))
            if tool_name == "get_object_by_track_id":
                return self._format_object(int(args["track_id"]))
            if tool_name == "get_objects_by_category":
                return self._format_category(self._canonical_category(args["category"]), limit=int(args.get("limit", 10)))
            if tool_name == "get_top_objects":
                return self._format_top_objects(n=int(args.get("n", 5)))
            if tool_name == "get_recent_objects":
                return self._format_recent_objects(limit=int(args.get("limit", 5)))
            if tool_name == "get_object_timeline":
                return self._format_object_timeline(int(args["track_id"]))
            if tool_name == "get_object_image_paths":
                return self._format_object_images(int(args["track_id"]))
            if tool_name == "get_category_timeline":
                return self._format_category_timeline(self._canonical_category(args["category"]))
            if tool_name == "get_category_windows":
                categories = args.get("categories", [])
                if isinstance(categories, str):
                    categories = [categories]
                return self._format_category_windows([self._canonical_category(c) for c in categories])
            if tool_name == "get_category_objects_coordinates":
                return self._format_category_coordinates(self._canonical_category(args["category"]))
            if tool_name == "get_category_proximity":
                target = self._canonical_category(args["target_category"])
                others = args.get("other_categories", args.get("other_category", []))
                if isinstance(others, str):
                    others = [others]
                radius = float(args.get("radius_m", 2.0))
                return self._format_category_proximity(target, [self._canonical_category(c) for c in others], radius)
            if tool_name == "get_inspection_timeline":
                return self._format_inspection_timeline()
            if tool_name == "get_temporal_clusters":
                return self._format_temporal_clusters(
                    window_ms=int(args.get("window_ms", 500)),
                    top_n=int(args.get("top_n", 10)),
                )
            if tool_name == "get_observation_counts_by_category":
                return self._format_observation_counts_by_category()
            if tool_name == "get_objects_in_time_range":
                return self._format_objects_in_time_range(
                    args["start_time"], args["end_time"], limit=int(args.get("limit", 50))
                )
            if tool_name == "get_observations_in_time_range":
                return self._format_observations_in_time_range(
                    args["start_time"], args["end_time"], limit=int(args.get("limit", 50))
                )
            if tool_name == "get_objects_near_position":
                return self._format_objects_near_position(
                    x=float(args["x"]),
                    y=float(args["y"]),
                    z=float(args["z"]),
                    radius_m=float(args.get("radius_m", 2.0)),
                    category=self._canonical_category(args["category"]) if args.get("category") else None,
                )
            if tool_name == "get_category_sample_images":
                return self._format_category_sample_images(
                    self._canonical_category(args["category"]), limit=int(args.get("limit", 5))
                )
            if tool_name == "get_inspection_poses":
                return self._format_inspection_poses(limit=int(args.get("limit", 20)))
            if tool_name == "get_filtered_objects":
                return self._format_filtered_objects(limit=int(args.get("limit", 50)))
            if tool_name == "get_object_distance":
                return self._format_object_distance(int(args["track_id_a"]), int(args["track_id_b"]))
            if tool_name == "get_category_bounding_box":
                return self._format_category_bounding_box(self._canonical_category(args["category"]))
            if tool_name == "get_category_observation_timeline":
                return self._format_category_observation_timeline(
                    self._canonical_category(args["category"]),
                    bucket_seconds=int(args.get("bucket_seconds", 60)),
                )
            if tool_name == "get_objects_by_category_in_time_range":
                return self._format_objects_by_category_in_time_range(
                    self._canonical_category(args["category"]),
                    args["start_time"],
                    args["end_time"],
                    limit=int(args.get("limit", 50)),
                )
            if tool_name == "get_object_movement":
                return self._format_object_movement(int(args["track_id"]))
            if tool_name == "get_nearest_objects_to_track":
                return self._format_nearest_objects_to_track(
                    int(args["track_id"]),
                    radius_m=float(args.get("radius_m", 2.0)),
                )
            if tool_name == "get_images_in_time_range":
                return self._format_images_in_time_range(
                    args["start_time"],
                    args["end_time"],
                    category=self._canonical_category(args["category"]) if args.get("category") else None,
                    limit=int(args.get("limit", 5)),
                )
            if tool_name == "get_category_cooccurrence":
                return self._format_category_cooccurrence(
                    window_ms=int(args.get("window_ms", 500)),
                    top_n=int(args.get("top_n", 10)),
                )
            if tool_name == "get_objects_in_temporal_cluster":
                return self._format_objects_in_temporal_cluster(
                    args["center_time"],
                    window_ms=int(args.get("window_ms", 500)),
                    limit=int(args.get("limit", 50)),
                )
            if tool_name == "run_sql_query":
                return self._format_sql_query_result(
                    args["query"],
                    limit=int(args.get("limit", 100)),
                )
            if tool_name == "get_report_summary":
                # Report context is fetched and injected by the LLM service.
                return None
        except Exception as exc:
            logger.warning("Tool execution failed for %s with args %s: %s", tool_name, args, exc)
        return None

    # ------------------------------------------------------------------
    # Formatters
    # ------------------------------------------------------------------

    def _format_summary(self) -> str:
        data = self.get_summary()
        lines = [
            f"Total aggregated objects: {data['total_objects']}",
        ]
        if data["categories"]:
            lines.append("Objects by category:")
            for row in data["categories"]:
                lines.append(f"- {row['category']}: {row['count']}")
        return "\n".join(lines)

    def _format_categories(self) -> str:
        categories = self.get_categories()
        if not categories:
            return "No categories found in the database."
        return "Known categories:\n" + "\n".join(f"- {c}" for c in categories)

    def _format_query_database(self, sql_query: str, limit: int = 100) -> str:
        return self._format_sql_query_result(sql_query, limit)

    def _format_category(self, category: str) -> str:
        objects = self.get_objects_by_category(category, limit=10)
        if not objects:
            return f"No objects found in category '{category}'."
        lines = [f"Found {len(objects)} object(s) in category '{category}':"]
        for obj in objects:
            lines.append(
                f"- Track {obj['track_id']}: {obj['observation_count']} observations, "
                f"{obj['total_point_count']} points, "
                f"centroid ({obj['centroid_x']:.2f}, {obj['centroid_y']:.2f}, {obj['centroid_z']:.2f})"
            )
        return "\n".join(lines)

    def _format_object(self, track_id: int) -> str:
        obj = self.get_object_by_track_id(track_id)
        if obj is None:
            return f"No object found with track ID {track_id}."
        lines = [
            f"Track ID: {obj['track_id']}",
            f"Category: {obj['category']}",
            f"Observations: {obj['observation_count']}",
            f"Total points: {obj['total_point_count']}",
            f"Centroid: ({obj['centroid_x']:.2f}, {obj['centroid_y']:.2f}, {obj['centroid_z']:.2f})",
        ]
        obs_count = len(obj.get("observations", []))
        if obs_count:
            lines.append(f"Per-frame observation rows: {obs_count}")
        return "\n".join(lines)

    def _format_top_objects(self, n: int = 5) -> str:
        objects = self.get_top_objects(n)
        if not objects:
            return "No objects found in the database."
        lines = [f"Top {len(objects)} objects by total point count:"]
        for obj in objects:
            lines.append(
                f"- Track {obj['track_id']} ({obj['category']}): "
                f"{obj['total_point_count']} points, "
                f"{obj['observation_count']} observations"
            )
        return "\n".join(lines)

    def _format_recent_objects(self, limit: int = 5) -> str:
        objects = self.get_recent_objects(limit)
        if not objects:
            return "No objects found in the database."
        lines = [f"{len(objects)} most recently seen object(s):"]
        for obj in objects:
            lines.append(
                f"- Track {obj['track_id']} ({obj['category']}): "
                f"last seen at {self._format_timestamp(obj['last_seen_ns'])}"
            )
        return "\n".join(lines)

    def _format_category_coordinates(self, category: str) -> str:
        objects = self.get_category_objects_with_coordinates(category)
        if not objects:
            return f"No objects found in category '{category}'."
        lines = [f"Objects in category '{category}' with coordinates (ordered by first appearance):"]
        for obj in objects:
            lines.append(
                f"- Track {obj['track_id']}: first seen {self._format_timestamp(obj['first_seen_ns'])}, "
                f"centroid ({obj['centroid_x']:.2f}, {obj['centroid_y']:.2f}, {obj['centroid_z']:.2f}), "
                f"bbox3d min ({obj['bbox3d_min_x']:.2f}, {obj['bbox3d_min_y']:.2f}, {obj['bbox3d_min_z']:.2f}), "
                f"max ({obj['bbox3d_max_x']:.2f}, {obj['bbox3d_max_y']:.2f}, {obj['bbox3d_max_z']:.2f})"
            )
        return "\n".join(lines)

    def _format_category_proximity(
        self,
        target_category: str,
        other_categories: list[str],
        radius_m: float = 2.0,
    ) -> str:
        results = self.get_category_proximity(target_category, other_categories, radius_m)
        if not results:
            return f"No proximity data found for '{target_category}' near {other_categories}."

        # Aggregate across all target objects.
        aggregate: dict[str, int] = {}
        detailed_lines = []
        for r in results:
            nearby = r["nearby"]
            for cat, cnt in nearby.items():
                aggregate[cat] = aggregate.get(cat, 0) + cnt
            nearby_str = ", ".join(f"{cnt} x {cat}" for cat, cnt in sorted(nearby.items(), key=lambda x: -x[1]))
            detailed_lines.append(
                f"- Track {r['track_id']} at ({r['centroid_x']:.2f}, {r['centroid_y']:.2f}, {r['centroid_z']:.2f}): "
                f"within {radius_m} m — {nearby_str or 'nothing nearby'}"
            )

        summary = ", ".join(f"{cnt} x {cat}" for cat, cnt in sorted(aggregate.items(), key=lambda x: -x[1]))
        lines = [
            f"Proximity summary for '{target_category}' within {radius_m} m of {', '.join(other_categories)}:",
            f"Total nearby objects: {summary or 'none'}",
            "Per-target breakdown:",
        ]
        lines.extend(detailed_lines)
        return "\n".join(lines)

    def _format_object_timeline(self, track_id: int) -> str:
        obj = self.get_object_by_track_id(track_id)
        if obj is None:
            return f"No object found with track ID {track_id}."

        first_ns = obj.get("first_seen_ns")
        last_ns = obj.get("last_seen_ns")
        duration_s = ((last_ns - first_ns) / 1e9) if first_ns and last_ns else 0.0

        lines = [
            f"Track {obj['track_id']} ({obj['category']}) timeline:",
            f"- First seen: {self._format_timestamp(first_ns)}",
            f"- Last seen:  {self._format_timestamp(last_ns)}",
            f"- Observations: {obj['observation_count']}",
            f"- Visible for about {self._format_duration(duration_s)}",
        ]

        observations = obj.get("observations", [])
        if observations:
            lines.append("Key moments:")
            step = max(1, len(observations) // 5)
            for i, obs in enumerate(observations):
                if i % step == 0 or i == len(observations) - 1:
                    lines.append(
                        f"  - {self._format_timestamp(obs['timestamp_ns'])}: "
                        f"{obs['point_count']} points at centroid "
                        f"({obs['centroid_x']:.2f}, {obs['centroid_y']:.2f}, {obs['centroid_z']:.2f})"
                    )

        image_urls = self.get_object_image_paths(track_id)
        if image_urls:
            lines.append("Object frames:")
            for url in image_urls[:10]:
                lines.append(f"![Track {track_id} frame]({self._image_url(url)})")
        return "\n".join(lines)

    def _format_object_images(self, track_id: int) -> str:
        obj = self.get_object_by_track_id(track_id)
        if obj is None:
            return f"No object found with track ID {track_id}."

        image_urls = self.get_object_image_paths(track_id)
        if not image_urls:
            return f"No images are stored for track {track_id}."

        lines = [
            f"Showing {min(len(image_urls), 10)} of {len(image_urls)} frames for track {track_id} ({obj['category']}):",
        ]
        for url in image_urls[:10]:
            lines.append(f"![Track {track_id} frame]({self._image_url(url)})")
        return "\n".join(lines)

    def _format_category_timeline(self, category: str) -> str:
        objects = self.get_category_timeline(category)
        if not objects:
            return f"No objects found in category '{category}'."

        lines = [f"Timeline for category '{category}' ({len(objects)} object(s)):"]
        for obj in objects:
            first_ns = obj.get("first_seen_ns")
            last_ns = obj.get("last_seen_ns")
            duration_s = ((last_ns - first_ns) / 1e9) if first_ns and last_ns else 0.0
            lines.append(
                f"- Track {obj['track_id']}: first seen {self._format_timestamp(first_ns)}, "
                f"last seen {self._format_timestamp(last_ns)}, "
                f"{obj['observation_count']} observations over {self._format_duration(duration_s)}"
            )
        return "\n".join(lines)

    def _format_category_windows(self, categories: list[str]) -> str:
        windows = self.get_category_windows(categories)
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

    def _format_inspection_timeline(self) -> str:
        objects = self.get_inspection_timeline()
        if not objects:
            return "No objects found in the database."

        first_ns = objects[0].get("first_seen_ns")
        last_ns = objects[-1].get("last_seen_ns")
        duration_s = ((last_ns - first_ns) / 1e9) if first_ns and last_ns else 0.0

        lines = [
            f"Full inspection timeline: {len(objects)} objects from {self._format_timestamp(first_ns)} to {self._format_timestamp(last_ns)} "
            f"({self._format_duration(duration_s)}).",
            "Chronological object log:",
        ]
        for obj in objects[:50]:
            lines.append(
                f"- {self._format_timestamp(obj.get('first_seen_ns'))}: Track {obj['track_id']} ({obj['category']}), "
                f"{obj['observation_count']} observations"
            )
        if len(objects) > 50:
            lines.append(f"... and {len(objects) - 50} more objects.")
        return "\n".join(lines)

    def _format_temporal_clusters(self, window_ms: int = 500, top_n: int = 10) -> str:
        clusters = self.get_temporal_clusters(window_ms=window_ms, top_n=top_n)
        if not clusters:
            return "No objects found in the database."

        lines = [
            f"Top {len(clusters)} busiest moments (objects grouped within {window_ms} ms):",
        ]
        for idx, cluster in enumerate(clusters, start=1):
            start = self._format_timestamp(cluster["start_ns"])
            end = self._format_timestamp(cluster["end_ns"])
            counts = ", ".join(f"{cnt} x {cat}" for cat, cnt in sorted(cluster["categories"].items(), key=lambda x: -x[1]))
            lines.append(
                f"{idx}. {start} to {end}: {cluster['object_count']} total objects — {counts}"
            )
        return "\n".join(lines)

    def _format_observation_counts_by_category(self) -> str:
        rows = self.get_observation_counts_by_category()
        if not rows:
            return "No observations found in the database."
        lines = ["Per-frame observation counts by category:"]
        for row in rows:
            lines.append(f"- {row['category']}: {row['count']} observations")
        return "\n".join(lines)

    def _format_objects_in_time_range(
        self, start_time: str | int | float, end_time: str | int | float, limit: int = 50
    ) -> str:
        objects = self.get_objects_in_time_range(start_time, end_time, limit)
        start_str = self._format_timestamp(self._parse_time_string(start_time))
        end_str = self._format_timestamp(self._parse_time_string(end_time))
        if not objects:
            return f"No objects found between {start_str} and {end_str}."
        lines = [f"{len(objects)} object(s) detected between {start_str} and {end_str}:"]
        for obj in objects:
            lines.append(
                f"- Track {obj['track_id']} ({obj['category']}): "
                f"{obj['observation_count']} observations, "
                f"centroid ({obj['centroid_x']:.2f}, {obj['centroid_y']:.2f}, {obj['centroid_z']:.2f})"
            )
        return "\n".join(lines)

    def _format_observations_in_time_range(
        self, start_time: str | int | float, end_time: str | int | float, limit: int = 50
    ) -> str:
        observations = self.get_observations_in_time_range(start_time, end_time, limit)
        start_str = self._format_timestamp(self._parse_time_string(start_time))
        end_str = self._format_timestamp(self._parse_time_string(end_time))
        if not observations:
            return f"No observations found between {start_str} and {end_str}."
        lines = [f"{len(observations)} observation(s) between {start_str} and {end_str}:"]
        for obs in observations:
            lines.append(
                f"- {self._format_timestamp(obs['timestamp_ns'])}: Track {obs['track_id']} ({obs['category']}), "
                f"{obs['point_count']} points at ({obs['centroid_x']:.2f}, {obs['centroid_y']:.2f}, {obs['centroid_z']:.2f})"
            )
        return "\n".join(lines)

    def _format_objects_near_position(
        self,
        x: float,
        y: float,
        z: float,
        radius_m: float = 2.0,
        category: str | None = None,
    ) -> str:
        objects = self.get_objects_near_position(x, y, z, radius_m, category)
        if not objects:
            cat_str = f" of category '{category}'" if category else ""
            return f"No objects{cat_str} found within {radius_m} m of ({x:.2f}, {y:.2f}, {z:.2f})."
        lines = [
            f"{len(objects)} object(s) within {radius_m} m of ({x:.2f}, {y:.2f}, {z:.2f}):"
        ]
        for obj in objects:
            lines.append(
                f"- Track {obj['track_id']} ({obj['category']}): "
                f"distance {obj['distance_m']:.2f} m, "
                f"centroid ({obj['centroid_x']:.2f}, {obj['centroid_y']:.2f}, {obj['centroid_z']:.2f})"
            )
        return "\n".join(lines)

    def _format_category_sample_images(self, category: str, limit: int = 5) -> str:
        image_urls = self.get_category_sample_images(category, limit)
        if not image_urls:
            return f"No sample images found for category '{category}'."
        lines = [f"Sample images for category '{category}':"]
        for url in image_urls:
            lines.append(f"![{category} sample]({self._image_url(url)})")
        return "\n".join(lines)

    def _format_inspection_poses(self, limit: int = 20) -> str:
        poses = self.get_inspection_poses(limit)
        if not poses:
            return "No inspection poses are recorded yet."
        lines = [f"First {len(poses)} inspection pose(s):"]
        for pose in poses:
            lines.append(
                f"- {pose['image_path']}: translation "
                f"({pose['tf_translation_x']:.2f}, {pose['tf_translation_y']:.2f}, {pose['tf_translation_z']:.2f}), "
                f"rotation ({pose['tf_rotation_x']:.2f}, {pose['tf_rotation_y']:.2f}, {pose['tf_rotation_z']:.2f}, {pose['tf_rotation_w']:.2f})"
            )
        return "\n".join(lines)

    def _format_filtered_objects(self, limit: int = 50) -> str:
        objects = self.get_filtered_objects(limit)
        if not objects:
            return "No filtered objects recorded."
        lines = [f"Most recent {len(objects)} filtered object(s):"]
        for obj in objects:
            lines.append(
                f"- Track {obj['track_id']} ({obj['category']}): reason='{obj['reason']}', "
                f"{obj['point_count']} points, "
                f"first seen {self._format_timestamp(obj['first_seen_ns'])}, "
                f"last seen {self._format_timestamp(obj['last_seen_ns'])}"
            )
        return "\n".join(lines)

    def _format_object_distance(self, track_id_a: int, track_id_b: int) -> str:
        result = self.get_object_distance(track_id_a, track_id_b)
        if result is None:
            return f"Could not find both tracks {track_id_a} and {track_id_b}."
        return (
            f"Distance between Track {result['track_id_a']} ({result['category_a']}) "
            f"and Track {result['track_id_b']} ({result['category_b']}): "
            f"{result['distance_m']:.2f} m"
        )

    def _format_category_bounding_box(self, category: str) -> str:
        bbox = self.get_category_bounding_box(category)
        if bbox is None:
            return f"No objects found in category '{category}'."
        lines = [
            f"Spatial extent for category '{category}' ({bbox['count']} object(s)):",
            f"- Centroid range: x [{bbox['min_cx']:.2f}, {bbox['max_cx']:.2f}], "
            f"y [{bbox['min_cy']:.2f}, {bbox['max_cy']:.2f}], "
            f"z [{bbox['min_cz']:.2f}, {bbox['max_cz']:.2f}]",
            f"- Bounding box min: ({bbox['min_x']:.2f}, {bbox['min_y']:.2f}, {bbox['min_z']:.2f})",
            f"- Bounding box max: ({bbox['max_x']:.2f}, {bbox['max_y']:.2f}, {bbox['max_z']:.2f})",
        ]
        return "\n".join(lines)

    def _format_category_observation_timeline(
        self, category: str, bucket_seconds: int = 60
    ) -> str:
        rows = self.get_category_observation_timeline(category, bucket_seconds)
        if not rows:
            return f"No observations found for category '{category}'."
        lines = [
            f"Observation timeline for '{category}' ({bucket_seconds}s buckets):"
        ]
        for row in rows:
            lines.append(
                f"- {self._format_timestamp(row['bucket_ns'])}: {row['count']} observations"
            )
        return "\n".join(lines)

    def _format_objects_by_category_in_time_range(
        self,
        category: str,
        start_time: str | int | float,
        end_time: str | int | float,
        limit: int = 50,
    ) -> str:
        objects = self.get_objects_by_category_in_time_range(category, start_time, end_time, limit)
        start_str = self._format_timestamp(self._parse_time_string(start_time))
        end_str = self._format_timestamp(self._parse_time_string(end_time))
        if not objects:
            return f"No {category} objects found between {start_str} and {end_str}."
        lines = [f"{len(objects)} '{category}' object(s) between {start_str} and {end_str}:"]
        for obj in objects:
            lines.append(
                f"- Track {obj['track_id']}: {obj['observation_count']} observations, "
                f"centroid ({obj['centroid_x']:.2f}, {obj['centroid_y']:.2f}, {obj['centroid_z']:.2f})"
            )
        return "\n".join(lines)

    def _format_object_movement(self, track_id: int) -> str:
        points = self.get_object_movement(track_id)
        if not points:
            return f"No movement data found for track {track_id}."
        start = points[0]
        end = points[-1]
        dx = end["centroid_x"] - start["centroid_x"]
        dy = end["centroid_y"] - start["centroid_y"]
        dz = end["centroid_z"] - start["centroid_z"]
        displacement = (dx * dx + dy * dy + dz * dz) ** 0.5
        lines = [
            f"Movement path for track {track_id} ({len(points)} observations):",
            f"- Start: {self._format_timestamp(start['timestamp_ns'])} at ({start['centroid_x']:.2f}, {start['centroid_y']:.2f}, {start['centroid_z']:.2f})",
            f"- End:   {self._format_timestamp(end['timestamp_ns'])} at ({end['centroid_x']:.2f}, {end['centroid_y']:.2f}, {end['centroid_z']:.2f})",
            f"- Displacement: {displacement:.2f} m",
        ]
        if len(points) > 2:
            lines.append("Key waypoints:")
            step = max(1, len(points) // 5)
            for i in range(0, len(points), step):
                p = points[i]
                lines.append(
                    f"  - {self._format_timestamp(p['timestamp_ns'])}: "
                    f"({p['centroid_x']:.2f}, {p['centroid_y']:.2f}, {p['centroid_z']:.2f})"
                )
        return "\n".join(lines)

    def _format_nearest_objects_to_track(self, track_id: int, radius_m: float = 2.0) -> str:
        results = self.get_nearest_objects_to_track(track_id, radius_m)
        if not results:
            return f"No objects found within {radius_m} m of track {track_id}."
        lines = [f"Objects within {radius_m} m of track {track_id}:"]
        for r in results:
            lines.append(
                f"- Track {r['track_id']} ({r['category']}): "
                f"{r['distance_m']:.2f} m away at "
                f"({r['centroid_x']:.2f}, {r['centroid_y']:.2f}, {r['centroid_z']:.2f})"
            )
        return "\n".join(lines)

    def _format_images_in_time_range(
        self,
        start_time: str | int | float,
        end_time: str | int | float,
        category: str | None = None,
        limit: int = 5,
    ) -> str:
        image_urls = self.get_images_in_time_range(start_time, end_time, category, limit)
        start_str = self._format_timestamp(self._parse_time_string(start_time))
        end_str = self._format_timestamp(self._parse_time_string(end_time))
        if not image_urls:
            cat_str = f" of category '{category}'" if category else ""
            return f"No images{cat_str} found between {start_str} and {end_str}."
        lines = [f"Sample images between {start_str} and {end_str}{' (' + category + ')' if category else ''}:"]
        for url in image_urls:
            lines.append(f"![frame]({self._image_url(url)})")
        return "\n".join(lines)

    def _format_category_cooccurrence(
        self, window_ms: int = 500, top_n: int = 10
    ) -> str:
        pairs = self.get_category_cooccurrence(window_ms, top_n)
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
        limit: int = 50,
    ) -> str:
        data = self.get_objects_in_temporal_cluster(center_time, window_ms, limit)
        start_str = self._format_timestamp(data["start_time_ns"])
        end_str = self._format_timestamp(data["end_time_ns"])
        if not data["objects"] and not data["observations"]:
            return f"No objects detected around {self._format_timestamp(data['center_time_ns'])}."
        lines = [
            f"Objects detected around {self._format_timestamp(data['center_time_ns'])} "
            f"({start_str} to {end_str}):",
            "Observation counts by category:",
        ]
        for cat, cnt in sorted(data["category_counts"].items(), key=lambda x: -x[1]):
            lines.append(f"- {cat}: {cnt}")
        lines.append("Objects with coordinates:")
        for obj in data["objects"][:20]:
            lines.append(
                f"- Track {obj['track_id']} ({obj['category']}): "
                f"centroid ({obj['centroid_x']:.2f}, {obj['centroid_y']:.2f}, {obj['centroid_z']:.2f}), "
                f"{obj['observation_count']} observations"
            )
        if len(data["objects"]) > 20:
            lines.append(f"... and {len(data['objects']) - 20} more objects.")
        return "\n".join(lines)

    def _format_sql_query_result(self, query: str, limit: int = 100) -> str:
        result = self.run_sql_query(query, limit)
        if "error" in result:
            return f"SQL query failed: {result['error']}\nQuery: {result['query']}"

        timestamp_cols = {"first_seen_ns", "last_seen_ns", "timestamp_ns", "bucket_ns", "first_seen", "last_seen"}

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
