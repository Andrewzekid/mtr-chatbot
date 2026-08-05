from __future__ import annotations

import logging
import math
import queue
import re
import socket
import threading
import time
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
        self._coord_seq = 0
        # Diagnostic counters for tracking viewer staleness.
        self._jobs_run = 0
        self._jobs_ok = 0
        self._jobs_failed = 0
        self._last_job_at: float | None = None
        self._last_ok_at: float | None = None
        # Force a fresh InspectionMarker after this many jobs to avoid a stale
        # gRPC sink that still passes the TCP probe.  Chosen conservatively: it
        # costs ~one reconnection every few minutes in an active chat session.
        self._marker_ttl_jobs = 20

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def highlight(
        self,
        *,
        object_ids: list[int] | None = None,
        coordinates: list[dict[str, float]] | None = None,
        category: str | None = None,
        categories: list[str] | None = None,
        inspection_id: int | None = None,
        label: str | None = None,
        keep_existing: bool = False,
    ) -> str:
        """Highlight objects / categories / raw coordinates in the Rerun viewer.

        Returns a short human-readable status string for the answering LLM to cite.
        Never raises, never blocks on the viewer: the actual Rerun logging happens
        asynchronously on a background thread.
        """
        if not self.settings.rerun_enabled:
            return "Rerun visualization is disabled (RERUN_ENABLED=false)."

        categories = list(categories) if categories else []
        if category:
            categories.append(category)
        categories = [c for c in categories if c]

        if not object_ids and not coordinates and not categories and inspection_id is None:
            return "Nothing to highlight."

        listening = self._viewer_listening()
        needs_spawn = self.settings.rerun_auto_spawn and not listening

        self._enqueue(
            {
                "object_ids": object_ids or [],
                "coordinates": coordinates or [],
                "categories": categories,
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
        n_categories = len(categories)
        if n_categories and n_objects:
            cats = ", ".join(f"'{c}'" for c in categories)
            return (
                f"Highlighted categor{'y' if n_categories == 1 else 'ies'} {cats} "
                f"and {n_objects} object(s){label_str} in {where}."
            )
        if n_categories:
            cats = ", ".join(f"'{c}'" for c in categories)
            return f"Highlighted categor{'y' if n_categories == 1 else 'ies'} {cats}{label_str} in {where}."
        if n_objects:
            return f"Highlighted {n_objects} object(s){label_str} in {where}."
        if coordinates:
            return f"Highlighted {len(coordinates)} coordinate(s){label_str} in {where}."
        return f"Highlighted selection{label_str} in {where}."

    def clear(self) -> str:
        """Clear all markings from the Rerun viewer.

        Returns a short status string. The actual Rerun clear happens
        asynchronously on the background worker thread.
        """
        if not self.settings.rerun_enabled:
            return "Rerun visualization is disabled (RERUN_ENABLED=false)."
        self._enqueue({"clear": True})
        return "Cleared all markings from the Rerun viewer."

    def plot_path(
        self,
        waypoints: list[dict[str, float]],
        label: str | None = None,
    ) -> str:
        """Plot a series of waypoints as spheres connected by line segments.

        Each waypoint dict should have x, y, z, and optionally label and color.
        """
        if not self.settings.rerun_enabled:
            return "Rerun visualization is disabled (RERUN_ENABLED=false)."
        if not waypoints:
            return "No waypoints to plot."

        listening = self._viewer_listening()
        needs_spawn = self.settings.rerun_auto_spawn and not listening

        self._enqueue(
            {
                "plot_path": True,
                "waypoints": waypoints,
                "label": label,
                "needs_spawn": needs_spawn,
            }
        )

        where = (
            "a Rerun viewer (launching now)"
            if needs_spawn
            else "the Rerun viewer"
        )
        return f"Plotted {len(waypoints)} waypoint(s) with connecting path in {where}."

    def job_stats(self) -> dict[str, Any]:
        """Diagnostic counters for the background highlight worker."""
        with self._lock:
            return {
                "jobs_run": self._jobs_run,
                "jobs_ok": self._jobs_ok,
                "jobs_failed": self._jobs_failed,
                "last_job_at": self._last_job_at,
                "last_ok_at": self._last_ok_at,
                "marker_ttl_jobs": self._marker_ttl_jobs,
            }

    def status(self) -> dict[str, Any]:
        """Console-facing viewer health snapshot (drives the UI connection chip).

        ``listening`` is a cheap TCP probe on the configured address; ``enabled``
        reflects the RERUN_ENABLED flag. Never blocks for long (0.5 s probe).
        """
        job_stats = self.job_stats()
        return {
            "enabled": bool(self.settings.rerun_enabled),
            "listening": self._viewer_listening(),
            "viewer_addr": self.settings.rerun_viewer_addr,
            "auto_spawn": bool(self.settings.rerun_auto_spawn),
            "app_id": self.settings.rerun_app_id,
            "job_stats": job_stats,
        }

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
        with self._lock:
            self._jobs_run += 1
            self._last_job_at = time.time()
            run_idx = self._jobs_run

        logger.info(
            "Rerun highlight job #%d started: keep_existing=%s coords=%d objects=%d categories=%s",
            run_idx,
            job.get("keep_existing", False),
            len(job.get("coordinates") or []),
            len(job.get("object_ids") or []),
            job.get("categories") or [],
        )
        marker = self._get_marker(job.get("needs_spawn", False))
        if marker is None:
            logger.info("InspectionMarker unavailable; skipping highlight job #%d", run_idx)
            with self._lock:
                self._jobs_failed += 1
            return

        try:
            if job.get("clear"):
                marker.clean_markings()
                with self._lock:
                    self._jobs_ok += 1
                    self._last_ok_at = time.time()
                logger.info("Rerun highlight job #%d: cleared all markings", run_idx)
                return

            if job.get("plot_path"):
                waypoints = job.get("waypoints") or []
                if waypoints:
                    import rerun as rr
                    with self._lock:
                        self._coord_seq += 1
                        seq = self._coord_seq
                    suffix = f"path_{seq}_{int(time.time() * 1000) % 1000000}"
                    # Plot waypoints as spheres
                    coords = [
                        {
                            "x": float(w["x"]),
                            "y": float(w.get("y", 0)),
                            "z": float(w.get("z", 0)),
                            "label": w.get("label", f"WP{i+1}"),
                            "radius": 0.3,
                            "color": [227, 0, 44],
                        }
                        for i, w in enumerate(waypoints)
                    ]
                    self._log_coordinates(marker, coords, entity_suffix=suffix)
                    # Plot connecting line segments
                    pts = [[float(w["x"]), float(w.get("y", 0)), float(w.get("z", 0))] for w in waypoints]
                    if len(pts) >= 2:
                        line_strips = [pts]  # single strip with all points
                        rr.log(
                            f"{marker.marks_root}/path_lines/{suffix}",
                            rr.LineStrips3D(line_strips),
                            colors=[[227, 0, 44, 255]],
                            radii=[0.15],
                        )
                    logger.info(
                        "Rerun plot_path job #%d: plotted %d waypoints with lines under %s",
                        run_idx, len(waypoints), suffix,
                    )
                with self._lock:
                    self._jobs_ok += 1
                    self._last_ok_at = time.time()
                return

            if not job.get("keep_existing", False):
                marker.clean_markings()
                logger.info("Rerun highlight job #%d: cleared previous markings", run_idx)

            for cat in job.get("categories") or []:
                marker.mark_by_category(cat, inspection_id=job.get("inspection_id"))

            object_ids = job.get("object_ids") or []
            if object_ids:
                marker.mark_by_ids(object_ids)

            coordinates = job.get("coordinates") or []
            if coordinates:
                with self._lock:
                    self._coord_seq += 1
                    seq = self._coord_seq
                base = re.sub(r"[^a-zA-Z0-9_]+", "_", str(job.get("label") or "highlight")).strip("_")[:32]
                suffix = f"{base}_{seq}_{int(time.time() * 1000) % 1000000}"
                self._log_coordinates(marker, coordinates, entity_suffix=suffix)
                logger.info(
                    "Rerun highlight job #%d: logged %d coordinate(s) under %s/coordinates/%s",
                    run_idx, len(coordinates), marker.marks_root, suffix,
                )

            with self._lock:
                self._jobs_ok += 1
                self._last_ok_at = time.time()
            logger.info(
                "Rerun highlight job #%d finished OK (ok=%d failed=%d)",
                run_idx, self._jobs_ok, self._jobs_failed,
            )
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._jobs_failed += 1
            self._marker = None  # recreate on next job after failure
            logger.warning("Rerun highlight job #%d failed: %s", run_idx, exc)

    def _get_marker(self, needs_spawn: bool) -> InspectionMarker | None:
        # If we already have a marker, verify the viewer is still reachable.
        # Rerun gRPC connections can drop (e.g. the viewer process was closed or
        # the machine went to sleep).  Re-using a dead marker silently loses
        # subsequent highlights, so recreate when the viewer is gone.
        #
        # We also periodically force recreation based on job count: a stale gRPC
        # sink can still pass the TCP probe while the viewer stops processing new
        # logs.  Recreating the marker rebuilds the SDK recording + sink, which
        # reliably refreshes the connection.
        force_recreate = False
        with self._lock:
            if self._jobs_run > 0 and self._jobs_run % self._marker_ttl_jobs == 0:
                force_recreate = True
                logger.info(
                    "Forcing InspectionMarker recreation after %d jobs",
                    self._jobs_run,
                )

        if self._marker is not None:
            if not force_recreate and self._viewer_listening():
                logger.debug("Reusing existing InspectionMarker (viewer still listening)")
                return self._marker
            reason = "viewer not listening" if not self._viewer_listening() else "periodic TTL"
            logger.info("Existing InspectionMarker's %s; recreating marker", reason)
            try:
                self._marker.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Failed to close stale InspectionMarker: %s", exc)
            self._marker = None

        try:
            # If the caller asked us to spawn, or the viewer isn't listening and
            # auto-spawn is enabled, spawn a fresh viewer. Otherwise try to connect
            # to the configured address.
            spawn = needs_spawn or (self.settings.rerun_auto_spawn and not self._viewer_listening())
            self._marker = InspectionMarker(
                db_path=self.settings.inspection_db_path,
                map_path=self.settings.rerun_map_pcd_path,
                objects_dir=self.settings.inspection_objects_dir,
                voxel=0.1,
                spawn_viewer=spawn,
                connect_url=None if spawn else self.settings.rerun_viewer_addr,
                load_map=True,
                leveling_rpy_deg=_parse_leveling(self.settings.rerun_leveling_rpy_deg),
                app_id=self.settings.rerun_app_id,
            )
            logger.info(
                "InspectionMarker ready (app=%s, spawn=%s)",
                self.settings.rerun_app_id,
                spawn,
            )
            return self._marker
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to create InspectionMarker: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Coordinate helper
    # ------------------------------------------------------------------

    @staticmethod
    def _log_coordinates(
        marker: InspectionMarker,
        coordinates: list[dict[str, float]],
        entity_suffix: str | None = None,
    ) -> None:
        pts: list[list[float]] = []
        labels: list[str] = []
        colors: list[list[int]] = []
        radii: list[float] = []
        for c in coordinates:
            try:
                x, y, z = float(c["x"]), float(c["y"]), float(c["z"])
            except (KeyError, TypeError, ValueError):
                continue
            pts.append([x, y, z])
            labels.append(str(c.get("label") or ""))
            try:
                radii.append(float(c.get("radius")) if c.get("radius") is not None else 0.15)
            except (TypeError, ValueError):
                radii.append(0.15)
            color = c.get("color")
            if isinstance(color, (list, tuple)) and len(color) in (3, 4):
                rgba = [int(v) for v in color]
                colors.append(rgba + [255] if len(rgba) == 3 else rgba)
            else:
                colors.append(list(_HIGHLIGHT_COLOR))
        if not pts:
            return
        pts_arr = np.asarray(pts, dtype=np.float32)
        kwargs: dict[str, Any] = {"labels": labels, "show_labels": True} if any(labels) else {}
        # Log each highlight under a unique sub-path so a Clear on marks_root
        # followed by a new Points3D cannot collide at the same entity path.
        # A unique suffix also makes consecutive highlights robust when the
        # viewer has stale state from a previous connection.
        suffix = re.sub(r"[^a-zA-Z0-9_]+", "_", str(entity_suffix or f"highlight_{int(time.time() * 1000) % 1000000}")).strip("_")[:48]
        rr.log(
            f"{marker.marks_root}/coordinates/{suffix}",
            rr.Points3D(
                pts_arr,
                colors=np.asarray(colors, dtype=np.uint8),
                radii=radii,
                **kwargs,
            ),
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
