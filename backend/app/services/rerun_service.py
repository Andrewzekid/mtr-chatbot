from __future__ import annotations

import logging
import math
import sqlite3
from pathlib import Path
from typing import Any

from app.config import Settings

logger = logging.getLogger(__name__)

# Faint color (RGBA 0-255) for the context object cloud; highlights use bright red.
_CONTEXT_COLOR = [120, 120, 120, 255]
_HIGHLIGHT_COLOR = [255, 40, 40, 255]
_TRAJECTORY_COLOR = [60, 160, 255, 255]


def _rpy_deg_to_rotmat(roll_deg: float, pitch_deg: float, yaw_deg: float) -> list[list[float]]:
    """ZYX (yaw·pitch·roll) rotation matrix from roll/pitch/yaw in degrees.

    Mirrors ``inspection_grounding``'s ``rpy_deg_to_quaternion`` + ``quat_to_rotmat``
    so highlights land in the same leveled world frame as the grounding bridge's
    ``world/bboxes3d``. Pure Python (no numpy) so this module stays dependency-light.
    """
    r = math.radians(roll_deg)
    p = math.radians(pitch_deg)
    y = math.radians(yaw_deg)
    cr, sr = math.cos(r), math.sin(r)
    cp, sp = math.cos(p), math.sin(p)
    cy, sy = math.cos(y), math.sin(y)
    # R = Rz(yaw) @ Ry(pitch) @ Rx(roll)
    return [
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp, cp * sr, cp * cr],
    ]


def _apply_rot(R: list[list[float]], xyz: list[float]) -> list[float]:
    x, y, z = xyz
    return [
        R[0][0] * x + R[0][1] * y + R[0][2] * z,
        R[1][0] * x + R[1][1] * y + R[1][2] * z,
        R[2][0] * x + R[2][1] * y + R[2][2] * z,
    ]


class RerunVisualizer:
    """Pushes 3D highlights to a separately-running Rerun viewer over TCP.

    The chatbot's ``highlight_in_rerun`` tool delegates here. Everything is best-effort:
    a missing ``rerun-sdk``, a disabled flag, or a viewer that is not running all degrade
    to a short status string instead of failing the chat turn.

    Highlights share the grounding pipeline's Rerun app id and world frame: object
    centroids/bboxes are pre-rotated by the leveling matrix (``rerun_leveling_rpy_deg``,
    matching the grounding ``rerun_bridge_node`` ``leveling_rpy_deg``) so they overlay the
    grounding map/bboxes rather than appearing in the tilted camera_init frame.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._conn: sqlite3.Connection | None = None
        self._connected = False
        self._rr: Any = None  # the rerun module, imported lazily
        self._tick = 0
        self._spawned = False  # True if we launched the viewer ourselves (vs. connected to a running one)
        self._leveling_R = self._parse_leveling(settings.rerun_leveling_rpy_deg)

    @staticmethod
    def _parse_leveling(rpy_str: str) -> list[list[float]]:
        """Parse a comma-separated "roll,pitch,yaw" degrees string into a 3x3 matrix."""
        try:
            parts = [float(v.strip()) for v in (rpy_str or "").split(",") if v.strip()]
        except ValueError:
            logger.warning("Invalid rerun_leveling_rpy_deg %r; using identity", rpy_str)
            return _rpy_deg_to_rotmat(0.0, 0.0, 0.0)
        if len(parts) != 3:
            logger.warning("rerun_leveling_rpy_deg needs 3 values, got %r; using identity", rpy_str)
            return _rpy_deg_to_rotmat(0.0, 0.0, 0.0)
        return _rpy_deg_to_rotmat(parts[0], parts[1], parts[2])

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def highlight(
        self,
        *,
        object_ids: list[int] | None = None,
        coordinates: list[dict[str, float]] | None = None,
        category: str | None = None,
        inspection_id: int | None = None,
        label: str | None = None,
    ) -> str:
        """Highlight objects / raw coordinates in the running Rerun viewer.

        Returns a short human-readable status string for the answering LLM to cite.
        Never raises.
        """
        if not self.settings.rerun_enabled:
            return "Rerun visualization is disabled (RERUN_ENABLED=false)."

        rr = self._load_rerun()
        if rr is None:
            return "Rerun visualization unavailable: the rerun-sdk package is not installed."

        points, boxes, labels = self._resolve_highlights(
            object_ids=object_ids, coordinates=coordinates, category=category,
            inspection_id=inspection_id,
        )
        if not points:
            return "No 3D coordinates to highlight (the referenced objects have no centroid)."

        if not self._ensure_connected(rr):
            return (
                f"Rerun viewer not reachable at {self.settings.rerun_viewer_addr} and auto-spawn "
                "failed — start it with `rerun` and ask again."
            )

        try:
            self._log_scene(rr, inspection_id=inspection_id)
            self._log_highlights(rr, points, boxes, labels, label=label)
        except Exception as exc:  # noqa: BLE001
            self._connected = False
            logger.warning("Rerun logging failed: %s", exc)
            return f"Rerun highlighting failed: {exc}"

        label_str = f" ({label})" if label else ""
        where = "a spawned Rerun viewer" if self._spawned else f"the Rerun viewer at {self.settings.rerun_viewer_addr}"
        n_points = len(points)
        n_boxes = len(boxes)
        if n_boxes:
            return (
                f"Highlighted {n_boxes} object(s){label_str} in {where} (grounding world frame)."
            )
        return (
            f"Highlighted {n_points} coordinate(s){label_str} in {where} (grounding world frame)."
        )

    # ------------------------------------------------------------------
    # Rerun connection / import
    # ------------------------------------------------------------------

    def _load_rerun(self) -> Any:
        if self._rr is not None:
            return self._rr
        try:
            import rerun as rr  # type: ignore
        except Exception as exc:  # noqa: BLE001
            logger.info("rerun-sdk not available: %s", exc)
            return None
        self._rr = rr
        return rr

    def _ensure_connected(self, rr: Any) -> bool:
        if self._connected:
            return True
        app_id = self.settings.rerun_app_id
        # First, try to attach to an already-running viewer on the configured TCP address.
        try:
            rr.init(app_id)
            rr.connect_tcp(self.settings.rerun_viewer_addr)
            self._log_world_frame(rr)
            self._connected = True
            self._spawned = False
            logger.info(
                "Connected to running Rerun viewer at %s (app=%s)",
                self.settings.rerun_viewer_addr, app_id,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.info("Rerun TCP connect failed at %s: %s", self.settings.rerun_viewer_addr, exc)

        # No viewer running: optionally launch one so highlighting "just works" the first
        # time the assistant has something to visualize.
        if self.settings.rerun_auto_spawn:
            try:
                rr.init(app_id)
                rr.spawn(connect=True)
                self._log_world_frame(rr)
                self._connected = True
                self._spawned = True
                logger.info("Spawned a Rerun viewer (app=%s)", app_id)
                return True
            except Exception as exc:  # noqa: BLE001
                logger.info("Rerun spawn failed: %s", exc)

        self._connected = False
        return False

    def _log_world_frame(self, rr: Any) -> None:
        """Log the static world view convention (matches the grounding bridge)."""
        try:
            rr.log("world", rr.ViewCoordinates.RIGHT_HAND_Z_UP, static=True)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # DB access (read-only, own connection)
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            path = Path(self.settings.inspection_db_path)
            # If the DB is missing, open anyway; sqlite raises on first use and the
            # broad except in highlight() turns that into a friendly status string.
            self._conn = sqlite3.connect(str(path))
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _resolve_highlights(
        self,
        *,
        object_ids: list[int] | None,
        coordinates: list[dict[str, float]] | None,
        category: str | None,
        inspection_id: int | None,
    ) -> tuple[list[list[float]], list[dict[str, Any]], list[str]]:
        """Return (points, boxes, labels) to highlight, in the LEVELED world frame.

        points: [[x,y,z], ...] pre-rotated centroids to mark as bright dots.
        boxes:  dicts with center/half_size/label for object 3D bboxes (center pre-rotated,
               half-sizes left axis-aligned, matching the grounding world/bboxes3d convention).
        labels: per-point text labels.
        """
        points: list[list[float]] = []
        boxes: list[dict[str, Any]] = []
        labels: list[str] = []
        R = self._leveling_R

        # Raw coordinates requested by the user (already in world/camera_init frame).
        for c in coordinates or []:
            try:
                x, y, z = float(c["x"]), float(c["y"]), float(c["z"])
            except (KeyError, TypeError, ValueError):
                continue
            points.append(_apply_rot(R, [x, y, z]))
            labels.append(str(c.get("label") or f"({x:.2f}, {y:.2f}, {z:.2f})"))

        # Objects by id and/or by category.
        ids = list(object_ids or [])
        where_clauses: list[str] = []
        params: list[Any] = []
        if ids:
            placeholders = ",".join("?" for _ in ids)
            where_clauses.append(f"o.id IN ({placeholders})")
            params.extend(ids)
        if category:
            where_clauses.append("c.name = ?")
            params.append(category)
        if inspection_id is not None:
            where_clauses.append(
                "o.id IN (SELECT d.object_id FROM detections d "
                "JOIN images i ON i.id=d.image_id WHERE i.inspection_id=?)"
            )
            params.append(inspection_id)

        if where_clauses:
            sql = (
                "SELECT o.id, c.name AS category, "
                "o.centroid_x, o.centroid_y, o.centroid_z, "
                "o.min_x, o.min_y, o.min_z, o.max_x, o.max_y, o.max_z "
                "FROM objects o JOIN categories c ON c.id=o.category_id "
                "WHERE " + " AND ".join(where_clauses)
            )
            try:
                rows = self._connect().execute(sql, params).fetchall()
            except sqlite3.Error as exc:
                logger.warning("Rerun highlight object query failed: %s", exc)
                rows = []
            for row in rows:
                cx, cy, cz = row["centroid_x"], row["centroid_y"], row["centroid_z"]
                if cx is None or cy is None or cz is None:
                    continue
                leveled = _apply_rot(R, [float(cx), float(cy), float(cz)])
                points.append(leveled)
                labels.append(f"Object {row['id']} ({row['category']})")
                # 3D bounding box if all corners are present. Center is pre-rotated into
                # the level world frame; half-sizes stay axis-aligned (grounding convention).
                if None not in (row["min_x"], row["min_y"], row["min_z"],
                                row["max_x"], row["max_y"], row["max_z"]):
                    center = [
                        (float(row["min_x"]) + float(row["max_x"])) / 2,
                        (float(row["min_y"]) + float(row["max_y"])) / 2,
                        (float(row["min_z"]) + float(row["max_z"])) / 2,
                    ]
                    boxes.append({
                        "center": _apply_rot(R, center),
                        "half_size": [
                            (float(row["max_x"]) - float(row["min_x"])) / 2,
                            (float(row["max_y"]) - float(row["min_y"])) / 2,
                            (float(row["max_z"]) - float(row["min_z"])) / 2,
                        ],
                        "label": f"Object {row['id']} ({row['category']})",
                    })
        return points, boxes, labels

    # ------------------------------------------------------------------
    # Scene logging
    # ------------------------------------------------------------------

    def _log_scene(self, rr: Any, *, inspection_id: int | None) -> None:
        """Log faint context: all object centroids (by category) + camera trajectory.

        All coordinates are pre-rotated by the leveling matrix so the context shares the
        leveled world frame with the highlights and the grounding map.
        """
        self._tick += 1
        R = self._leveling_R
        try:
            rr.set_time_sequence("turn", self._tick)
        except Exception:  # noqa: BLE001
            pass

        # Object centroids grouped by category (faint dots), pre-rotated to level world.
        try:
            rows = self._connect().execute(
                "SELECT c.name AS category, o.centroid_x, o.centroid_y, o.centroid_z "
                "FROM objects o JOIN categories c ON c.id=o.category_id "
                "WHERE o.centroid_x IS NOT NULL"
            ).fetchall()
            by_cat: dict[str, list[list[float]]] = {}
            for row in rows:
                by_cat.setdefault(row["category"], []).append(
                    _apply_rot(R, [float(row["centroid_x"]), float(row["centroid_y"]), float(row["centroid_z"])])
                )
            for cat, pts in by_cat.items():
                if not pts:
                    continue
                safe = cat.replace("/", "_").replace(" ", "_")
                rr.log(
                    f"world/context/objects/{safe}",
                    rr.Points3D(positions=pts, colors=[_CONTEXT_COLOR] * len(pts), radii=[0.05] * len(pts)),
                )
        except sqlite3.Error as exc:
            logger.warning("Rerun context object query failed: %s", exc)

        # Camera trajectory from image poses (optional inspection scope), pre-rotated.
        try:
            sql = (
                "SELECT tf_translation_x, tf_translation_y, tf_translation_z "
                "FROM images WHERE tf_translation_x IS NOT NULL"
            )
            params: list[Any] = []
            if inspection_id is not None:
                sql += " AND inspection_id = ?"
                params.append(inspection_id)
            rows = self._connect().execute(sql, params).fetchall()
            traj = [
                _apply_rot(R, [float(r["tf_translation_x"]), float(r["tf_translation_y"]), float(r["tf_translation_z"])])
                for r in rows
                if None not in (r["tf_translation_x"], r["tf_translation_y"], r["tf_translation_z"])
            ]
            if len(traj) >= 2:
                rr.log(
                    "world/context/camera_trajectory",
                    rr.LineStrips3D(strips=[traj], colors=[_TRAJECTORY_COLOR]),
                )
        except sqlite3.Error as exc:
            logger.warning("Rerun trajectory query failed: %s", exc)

    def _log_highlights(
        self,
        rr: Any,
        points: list[list[float]],
        boxes: list[dict[str, Any]],
        labels: list[str],
        *,
        label: str | None,
    ) -> None:
        path_suffix = (label or "selection").replace("/", "_").replace(" ", "_")
        base = f"world/highlights/{path_suffix}"

        if points:
            rr.log(
                f"{base}/points",
                rr.Points3D(
                    positions=points,
                    colors=[_HIGHLIGHT_COLOR] * len(points),
                    radii=[0.18] * len(points),
                    labels=labels,
                ),
            )
        if boxes:
            centers = [b["center"] for b in boxes]
            half_sizes = [b["half_size"] for b in boxes]
            box_labels = [b["label"] for b in boxes]
            rr.log(
                f"{base}/boxes",
                rr.Boxes3D(
                    centers=centers,
                    half_sizes=half_sizes,
                    colors=[_HIGHLIGHT_COLOR] * len(boxes),
                    labels=box_labels,
                ),
            )