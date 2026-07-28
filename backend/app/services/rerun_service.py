from __future__ import annotations

import logging
import math
import queue
import socket
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from app.config import Settings

logger = logging.getLogger(__name__)

# Faint color (RGBA 0-255) for the context object cloud; highlights use bright red.
_CONTEXT_COLOR = [120, 120, 120, 255]
_HIGHLIGHT_COLOR = [255, 40, 40, 255]
_TRAJECTORY_COLOR = [60, 160, 255, 255]
# Station map is loaded from a pre-extracted .npz with per-point RGBA colors,
# so no single map color constant is needed.
_MAP_POINT_RADIUS = 0.04


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
    """Pushes 3D highlights to a Rerun viewer, sharing the grounding pipeline's frame.

    The chatbot's ``highlight_in_rerun`` tool delegates here, and the answerer also
    auto-highlights any coordinates/object ids found in the tool results (no explicit
    tool call needed). Everything is best-effort:

    - All Rerun I/O runs on a single background daemon thread so a slow/unreachable
      viewer can never block the chat turn. ``highlight()`` only does a fast DB resolve
      + port probe, then enqueues the work and returns an optimistic status string.
    - A missing ``rerun-sdk``, a disabled flag, or a viewer that cannot be reached or
      spawned all degrade to a short status string; nothing raises into the chat.
    - Highlights/context are logged as ``static=True`` so they render immediately
      regardless of the viewer's timeline position (the cause of the "empty viewer"
      bug when logging at a non-zero ``set_time_sequence`` tick).

    Highlights share the grounding pipeline's app id and world frame: object
    centroids/bboxes are pre-rotated by the leveling matrix (``rerun_leveling_rpy_deg``,
    matching the grounding ``rerun_bridge_node`` ``leveling_rpy_deg``) so they overlay
    the grounding map/bboxes rather than appearing in the tilted camera_init frame.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._conn: sqlite3.Connection | None = None
        self._rr: Any = None  # the rerun module, imported lazily
        self._connected = False
        self._spawned = False  # True if we launched the viewer ourselves
        self._inited = False
        self._leveling_R = self._parse_leveling(settings.rerun_leveling_rpy_deg)
        self._lock = threading.Lock()
        self._db_lock = threading.Lock()  # sqlite connection is shared across threads
        self._queue: queue.SimpleQueue | None = None
        self._worker_started = False
        # Station map: lazily-loaded, pre-rotated point cloud (numpy (N,3) positions +
        # (N,4) uint8 RGBA colors, or positions only for legacy .npy) logged once per
        # viewer connection as a static `world/map` entity.
        self._map_points: Any = None
        self._map_loaded = False  # True once we've tried (success or skip) so we don't retry every job
        self._map_logged = False  # True once logged on the current connection

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
        """Highlight objects / raw coordinates in the Rerun viewer.

        Returns a short human-readable status string for the answering LLM to cite.
        Never raises, never blocks on the viewer: the actual Rerun logging happens
        asynchronously on a background thread.
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

        # Fast probe (non-blocking) to decide the status and whether we must spawn.
        listening = self._viewer_listening()
        if not listening and not self.settings.rerun_auto_spawn:
            return (
                f"Rerun viewer not reachable at {self.settings.rerun_viewer_addr} — "
                "start it with `rerun` and ask again."
            )

        # Hand the Rerun I/O to the background worker so the chat turn never blocks.
        self._enqueue({
            "points": points, "boxes": boxes, "labels": labels,
            "label": label, "inspection_id": inspection_id,
            "needs_spawn": not listening,
        })

        where = "the Rerun viewer" if listening else "a Rerun viewer (launching now)"
        label_str = f" ({label})" if label else ""
        n_boxes = len(boxes)
        if n_boxes:
            return f"Highlighted {n_boxes} object(s){label_str} in {where} (grounding world frame)."
        return f"Highlighted {len(points)} coordinate(s){label_str} in {where} (grounding world frame)."

    # ------------------------------------------------------------------
    # Background worker (all Rerun I/O happens here, off the chat turn)
    # ------------------------------------------------------------------

    def _enqueue(self, job: dict[str, Any]) -> None:
        with self._lock:
            if not self._worker_started:
                self._queue = queue.SimpleQueue()
                threading.Thread(target=self._worker_loop, daemon=True).start()
                self._worker_started = True
        self._queue.put(job)

    def _worker_loop(self) -> None:
        while True:
            job = self._queue.get()
            try:
                self._run_job(job)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Rerun worker job failed: %s", exc)

    def _run_job(self, job: dict[str, Any]) -> None:
        rr = self._rr
        if rr is None:
            return
        if not self._ensure_connected(rr, needs_spawn=job.get("needs_spawn", False)):
            logger.info("Rerun not connected; skipping highlight job")
            return
        try:
            self._log_map(rr)  # static station map, logged once per connection
            self._log_scene(rr, inspection_id=job.get("inspection_id"))
            self._log_highlights(rr, job["points"], job["boxes"], job["labels"], label=job.get("label"))
        except Exception as exc:  # noqa: BLE001
            self._connected = False
            self._map_logged = False  # re-log the map after a reconnect
            logger.warning("Rerun logging failed: %s", exc)

    # ------------------------------------------------------------------
    # Rerun import / connection
    # ------------------------------------------------------------------

    def _load_rerun(self) -> Any:
        with self._lock:
            if self._rr is not None:
                return self._rr
            try:
                import rerun as rr  # type: ignore
            except Exception as exc:  # noqa: BLE001
                logger.info("rerun-sdk not available: %s", exc)
                return None
            self._rr = rr
            return rr

    def _viewer_listening(self) -> bool:
        """Quick TCP probe: is a Rerun viewer listening on the configured address?"""
        host, _, port = self.settings.rerun_viewer_addr.partition(":")
        try:
            with socket.create_connection((host or "127.0.0.1", int(port) or 9876), timeout=0.5):
                return True
        except OSError:
            return False

    def _spawn_viewer(self) -> bool:
        """Launch a detached `rerun` viewer and wait (bounded) for its port to open."""
        host, _, port = self.settings.rerun_viewer_addr.partition(":")
        port = int(port) if port else 9876
        try:
            subprocess.Popen(
                ["rerun", "--port", str(port), "--memory-limit=75%", "--server-memory-limit=1GiB"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,  # fully detach so it never holds our pipes
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("Rerun viewer spawn (subprocess) failed: %s", exc)
            return False
        for _ in range(40):  # up to ~8 s for the viewer to bind
            if self._viewer_listening():
                return True
            time.sleep(0.2)
        logger.info("Rerun viewer did not start listening on port %s", port)
        return False

    def _ensure_connected(self, rr: Any, *, needs_spawn: bool) -> bool:
        if self._connected:
            return True
        app_id = self.settings.rerun_app_id
        if not self._inited:
            rr.init(app_id)
            self._inited = True

        # Attach to an already-running viewer. rerun 0.35 speaks gRPC; connect_grpc is
        # lazy and does NOT raise when nothing is listening, so probe the port first.
        if self._viewer_listening():
            try:
                rr.connect_grpc(f"rerun+http://{self.settings.rerun_viewer_addr}/proxy")
                self._log_world_frame(rr)
                self._connected = True
                self._spawned = False
                self._map_logged = False  # fresh connection: re-log the static map
                logger.info("Connected to running Rerun viewer at %s", self.settings.rerun_viewer_addr)
                return True
            except Exception as exc:  # noqa: BLE001
                logger.info("Rerun connect_grpc failed: %s", exc)

        # No viewer running: optionally launch one.
        if self.settings.rerun_auto_spawn and (needs_spawn or True):
            if self._spawn_viewer():
                try:
                    rr.connect_grpc(f"rerun+http://{self.settings.rerun_viewer_addr}/proxy")
                    self._log_world_frame(rr)
                    self._connected = True
                    self._spawned = True
                    self._map_logged = False  # fresh connection: re-log the static map
                    logger.info("Spawned + connected to Rerun viewer (app=%s)", app_id)
                    return True
                except Exception as exc:  # noqa: BLE001
                    logger.info("Rerun connect_grpc after spawn failed: %s", exc)

        self._connected = False
        return False

    def _log_world_frame(self, rr: Any) -> None:
        """Log the static world view convention (matches the grounding bridge)."""
        try:
            rr.log("world", rr.ViewCoordinates.RIGHT_HAND_Z_UP, static=True)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # Station map (static point cloud, logged once per connection)
    # ------------------------------------------------------------------

    def _load_map_points(self) -> Any:
        """Load + pre-rotate the station map .npz (cached). Returns positions + colors.

        The .npz holds raw camera_init-frame points (same frame as DB centroids) plus
        per-point RGBA uint8 colors. We pre-rotate the positions by the leveling matrix
        so the map shares the leveled world frame with the highlights and the grounding
        bridge's ``world/leveled`` map. Legacy `.npy` position-only files are also
        supported (they get a default gray color).

        numpy is a transitive dependency of rerun-sdk, so importing it here is safe.
        """
        if self._map_loaded:
            return self._map_points
        self._map_loaded = True  # only attempt once per process (success or skip)
        if not self.settings.rerun_map_enabled:
            return None
        path = Path(self.settings.rerun_map_points_path)
        if not path.is_file():
            logger.info("Station map not found at %s; skipping map overlay", path)
            return None
        try:
            import numpy as np  # type: ignore

            R = np.asarray(self._leveling_R, dtype=np.float32)
            if path.suffix.lower() == ".npz":
                data = np.load(path)
                raw = data["positions"].astype(np.float32, copy=False)
                colors = data["colors"]
                if colors.dtype != np.uint8:
                    colors = colors.astype(np.uint8, copy=False)
                if colors.ndim == 1:
                    # Packed uint32 colors - decode to RGBA.
                    # Rerun 0.35 experimental RRD reader returns 0xRRGGBBAA order.
                    rgba = np.empty((colors.shape[0], 4), dtype=np.uint8)
                    rgba[:, 0] = (colors >> 24) & 0xFF
                    rgba[:, 1] = (colors >> 16) & 0xFF
                    rgba[:, 2] = (colors >> 8) & 0xFF
                    rgba[:, 3] = (colors >> 0) & 0xFF
                    colors = rgba
                elif colors.ndim == 2 and colors.shape[1] == 4:
                    # Legacy station_map.npz files were saved with the channel
                    # order misidentified (alpha ended up in the R slot). Detect
                    # that case: if the first channel is saturated at 255 for
                    # almost every point while the last channel varies, the rows
                    # are actually [A, B, G, R] and need to be reordered to
                    # [R, G, B, A]. This auto-fixes existing broken .npz files.
                    red_saturated = (colors[:, 0] == 255).mean()
                    alpha_varying = colors[:, 3].std() > 1.0
                    if red_saturated > 0.95 and alpha_varying:
                        fixed = np.empty_like(colors)
                        fixed[:, 0] = colors[:, 3]  # R
                        fixed[:, 1] = colors[:, 2]  # G
                        fixed[:, 2] = colors[:, 1]  # B
                        fixed[:, 3] = colors[:, 0]  # A
                        colors = fixed
            else:
                # Legacy .npy: positions only.
                raw = np.load(path).astype(np.float32, copy=False)
                colors = np.full((raw.shape[0], 4), [128, 128, 128, 255], dtype=np.uint8)

            if raw.ndim != 2 or raw.shape[1] != 3:
                logger.warning("Station map has unexpected positions shape %s; skipping", raw.shape)
                return None
            if colors.shape[0] != raw.shape[0]:
                logger.warning(
                    "Station map color count %s != position count %s; using fallback gray",
                    colors.shape[0], raw.shape[0]
                )
                colors = np.full((raw.shape[0], 4), [128, 128, 128, 255], dtype=np.uint8)

            leveled = raw @ R.T  # (N,3) pre-rotated into the level world frame
            self._map_points = (leveled, colors)
            logger.info("Loaded station map: %s points from %s", raw.shape[0], path)
            return self._map_points
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load station map %s: %s", path, exc)
            return None

    def _log_map(self, rr: Any) -> None:
        """Log the station map as a static `world/map` Points3D (once per connection)."""
        if self._map_logged:
            return
        loaded = self._load_map_points()
        if loaded is None:
            self._map_logged = True  # disabled / missing / failed -> don't retry every job
            return
        pts, colors = loaded
        try:
            rr.log(
                "world/map",
                rr.Points3D(positions=pts, colors=colors, radii=_MAP_POINT_RADIUS),
                static=True,
            )
            self._map_logged = True
            logger.info("Logged station map (%s colored points) as world/map", pts.shape[0])
        except Exception as exc:  # noqa: BLE001
            logger.warning("Rerun map logging failed: %s", exc)

    # ------------------------------------------------------------------
    # DB access (read-only, own connection)
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            path = Path(self.settings.inspection_db_path)
            # If the DB is missing, open anyway; sqlite raises on first use and the
            # broad except in _run_job turns that into a logged warning. The connection
            # is shared between the main thread (resolve) and the worker thread (scene
            # logging), so allow cross-thread use and guard access with _db_lock.
            self._conn = sqlite3.connect(str(path), check_same_thread=False)
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
                with self._db_lock:
                    rows = self._connect().execute(sql, params).fetchall()
            except sqlite3.Error as exc:
                logger.warning("Rerun highlight object query failed: %s", exc)
                rows = []
            for row in rows:
                cx, cy, cz = row["centroid_x"], row["centroid_y"], row["centroid_z"]
                if cx is None or cy is None or cz is None:
                    continue
                points.append(_apply_rot(R, [float(cx), float(cy), float(cz)]))
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
    # Scene logging (all static so it renders regardless of timeline)
    # ------------------------------------------------------------------

    def _log_scene(self, rr: Any, *, inspection_id: int | None) -> None:
        """Log faint context: all object centroids (by category) + camera trajectory.

        All coordinates are pre-rotated by the leveling matrix and logged STATIC so the
        context shares the leveled world frame with the highlights and the grounding map
        and is always visible (no timeline scrubbing required).
        """
        R = self._leveling_R

        # Object centroids grouped by category (faint dots), pre-rotated to level world.
        try:
            with self._db_lock:
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
                    static=True,
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
            with self._db_lock:
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
                    static=True,
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
                static=True,
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
                static=True,
            )