from __future__ import annotations

import logging
import math
import queue
import socket
import threading
from typing import Any

import numpy as np
import rerun as rr

from app.config import Settings
from app.services.inspection_marking import InspectionMarker

logger = logging.getLogger(__name__)

# Bright red for raw coordinate highlights.
_HIGHLIGHT_COLOR = [255, 40, 40, 255]


def _parse_leveling(rpy_str: str) -> list[float]:
    """Parse a comma-separated "roll,pitch,yaw" degrees string into a 3-element list."""
    try:
        parts = [float(v.strip()) for v in (rpy_str or "").split(",") if v.strip()]
    except ValueError:
        logger.warning("Invalid rerun_leveling_rpy_deg %r; using identity", rpy_str)
        return [0.0, 0.0, 0.0]
    if len(parts) != 3:
        logger.warning(
            "rerun_leveling_rpy_deg needs 3 values, got %r; using identity", rpy_str
        )
        return [0.0, 0.0, 0.0]
    return parts


class RerunVisualizer:
    """Pushes 3D highlights to a Rerun viewer via ``InspectionMarker``.

    The chatbot's ``highlight_in_rerun`` tool delegates here. Everything is
    best-effort and non-blocking:

    - All heavy Rerun/PCD/DB work runs on a single background daemon thread so a
      slow/unreachable viewer can never block the chat turn. ``highlight()`` only
      does a fast port probe, then enqueues the work and returns an optimistic
      status string.
    - A missing ``rerun-sdk``, a disabled flag, or a viewer that cannot be reached
      or spawned all degrade to a logged warning; nothing raises into the chat.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._marker: InspectionMarker | None = None
        self._lock = threading.Lock()
        self._queue: queue.SimpleQueue | None = None
        self._worker_started = False

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
        keep_existing: bool = False,
    ) -> str:
        """Highlight objects / raw coordinates in the Rerun viewer.

        Returns a short human-readable status string for the answering LLM to cite.
        Never raises, never blocks on the viewer: the actual Rerun logging happens
        asynchronously on a background thread.
        """
        if not self.settings.rerun_enabled:
            return "Rerun visualization is disabled (RERUN_ENABLED=false)."

        if not object_ids and not coordinates and not category and inspection_id is None:
            return "Nothing to highlight."

        listening = self._viewer_listening()
        needs_spawn = self.settings.rerun_auto_spawn and not listening

        self._enqueue(
            {
                "object_ids": object_ids or [],
                "coordinates": coordinates or [],
                "category": category,
                "inspection_id": inspection_id,
                "label": label,
                "keep_existing": keep_existing,
                "needs_spawn": needs_spawn,
            }
        )

        where = (
            "a Rerun viewer (launching now)"
            if needs_spawn
            else "the Rerun viewer"
        )
        label_str = f" ({label})" if label else ""

        n_objects = len(object_ids) if object_ids else 0
        if category and n_objects:
            return (
                f"Highlighted category '{category}' and {n_objects} object(s)"
                f"{label_str} in {where}."
            )
        if category:
            return f"Highlighted category '{category}'{label_str} in {where}."
        if n_objects:
            return f"Highlighted {n_objects} object(s){label_str} in {where}."
        if coordinates:
            return f"Highlighted {len(coordinates)} coordinate(s){label_str} in {where}."
        return f"Highlighted selection{label_str} in {where}."

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
        marker = self._get_marker(job.get("needs_spawn", False))
        if marker is None:
            logger.info("InspectionMarker unavailable; skipping highlight job")
            return

        try:
            if not job.get("keep_existing", False):
                marker.clean_markings()

            if job.get("category"):
                marker.mark_by_category(job["category"])

            object_ids = job.get("object_ids") or []
            if object_ids:
                marker.mark_by_ids(object_ids)

            coordinates = job.get("coordinates") or []
            if coordinates:
                self._log_coordinates(marker, coordinates)
        except Exception as exc:  # noqa: BLE001
            self._marker = None  # recreate on next job after failure
            logger.warning("Rerun highlight job failed: %s", exc)

    def _get_marker(self, needs_spawn: bool) -> InspectionMarker | None:
        if self._marker is not None:
            return self._marker
        try:
            self._marker = InspectionMarker(
                db_path=self.settings.inspection_db_path,
                map_path=self.settings.rerun_map_pcd_path,
                objects_dir=self.settings.inspection_objects_dir,
                voxel=0.1,
                spawn_viewer=needs_spawn,
                connect_url=None if needs_spawn else self.settings.rerun_viewer_addr,
                load_map=True,
                leveling_rpy_deg=_parse_leveling(self.settings.rerun_leveling_rpy_deg),
                app_id=self.settings.rerun_app_id,
            )
            logger.info(
                "InspectionMarker ready (app=%s, spawn=%s)",
                self.settings.rerun_app_id,
                needs_spawn,
            )
            return self._marker
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to create InspectionMarker: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Coordinate helper
    # ------------------------------------------------------------------

    @staticmethod
    def _log_coordinates(marker: InspectionMarker, coordinates: list[dict[str, float]]) -> None:
        pts: list[list[float]] = []
        for c in coordinates:
            try:
                x, y, z = float(c["x"]), float(c["y"]), float(c["z"])
            except (KeyError, TypeError, ValueError):
                continue
            pts.append([x, y, z])
        if not pts:
            return
        pts_arr = np.asarray(pts, dtype=np.float32)
        colors = np.tile(_HIGHLIGHT_COLOR, (pts_arr.shape[0], 1)).astype(np.uint8)
        rr.log(
            f"{marker.base_path}/coordinates",
            rr.Points3D(pts_arr, colors=colors, radii=[0.12] * pts_arr.shape[0]),
        )

    # ------------------------------------------------------------------
    # Viewer probe
    # ------------------------------------------------------------------

    def _viewer_listening(self) -> bool:
        """Quick TCP probe: is a Rerun viewer listening on the configured address?"""
        host, _, port = self.settings.rerun_viewer_addr.partition(":")
        try:
            with socket.create_connection(
                (host or "127.0.0.1", int(port) or 9876), timeout=0.5
            ):
                return True
        except OSError:
            return False
