from __future__ import annotations

import csv
import io
import logging
import sqlite3
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Query

logger = logging.getLogger(__name__)


def _to_dict(value: Any) -> Any:
    """Recursively convert sqlite3.Row objects to plain dicts."""
    if isinstance(value, sqlite3.Row):
        return {k: _to_dict(value[k]) for k in value.keys()}
    if isinstance(value, dict):
        return {k: _to_dict(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_dict(item) for item in value]
    return value

_TIMESTAMP_INT_KEYS = {"started_at"}
_TIMESTAMP_NS_KEYS = {
    "timestamp_ns", "first_seen_ns", "last_seen_ns", "bucket_ns",
    "start_ns", "end_ns", "center_time_ns", "start_time_ns", "end_time_ns",
}
_IMAGE_PATH_KEYS = {"filename", "sample_image_path", "gt_filename", "inspection_filename"}


def _iso_ns(ns: Any) -> str | None:
    if ns is None:
        return None
    try:
        return datetime.fromtimestamp(int(ns) / 1e9).isoformat(timespec="milliseconds")
    except (OSError, OverflowError, ValueError):
        return str(ns)


def _normalize_row(db, value: Any) -> Any:
    """Recursively enrich a tool dict for the console.

    - ``*_ns`` ints get a sibling ``*_ns_iso`` ISO-8601 string (raw value kept).
    - image path columns get a sibling ``*_url`` pointing at the served image.
    """
    value = _to_dict(value)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(v, dict):
                out[k] = _normalize_row(db, v)
            elif isinstance(v, list):
                out[k] = [_normalize_row(db, item) for item in v]
            elif k in _TIMESTAMP_NS_KEYS and isinstance(v, int) and v > 0:
                out[k] = v
                out[f"{k}_iso"] = _iso_ns(v)
            elif k in _IMAGE_PATH_KEYS and v:
                out[k] = v
                out[f"{k}_url"] = db._image_url(v) if hasattr(db, "_image_url") else None
            else:
                out[k] = v
        return out
    if isinstance(value, list):
        return [_normalize_row(db, item) for item in value]
    return value


def _resolve_inspection(db, inspection_id: Any) -> int | None:
    if inspection_id is None or inspection_id in ("", "all"):
        return None
    try:
        return int(inspection_id)
    except (TypeError, ValueError):
        return None


def _not_configured() -> None:
    raise HTTPException(
        status_code=503,
        detail="Inspection database is not configured/available on this backend.",
    )


def _to_csv_rows(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "no rows\n"
    keys: list[str] = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=keys)
    writer.writeheader()
    for r in rows:
        writer.writerow({k: r.get(k, "") for k in keys})
    return buf.getvalue()


def build_api_router(db: Any, rerun: Any) -> APIRouter:
    """Build the button-driven console REST API over the existing tools.

    ``db`` is the ``InspectionDBClient`` instance and ``rerun`` is the
    ``RerunVisualizer`` instance (either may be ``None`` if not configured).
    """
    router = APIRouter(prefix="/api")

    def _rerun_status_dict() -> dict[str, Any]:
        if rerun is None:
            return {
                "enabled": False,
                "listening": False,
                "viewer_addr": None,
                "auto_spawn": False,
                "app_id": None,
                "job_stats": None,
            }
        return rerun.status()

    # ------------------------------------------------------------------
    # System / info
    # ------------------------------------------------------------------

    @router.get("/info")
    def info() -> dict[str, Any]:
        if db is None:
            return {"db_available": False, "db_path": None, "db_mtime": None,
                    "categories": [], "inspections": [], "total_objects": 0,
                    "anomaly_tables": False, "rerun": _rerun_status_dict(),
                    "time_range": None}
        return {
            "db_available": True,
            "db_path": str(db.db_path),
            "db_mtime": db.db_mtime(),
            "categories": db.get_categories(),
            "inspections": _normalize_row(db, db.get_inspections()),
            "category_counts": db._category_counts(),
            "total_objects": db.get_summary()["total_objects"],
            "anomaly_tables": db._anomaly_tables_exist(),
            "time_range": db._get_inspection_time_range_ns(),
            "rerun": _rerun_status_dict(),
        }

    @router.get("/rerun/status")
    def rerun_status() -> dict[str, Any]:
        return _rerun_status_dict()

    # ------------------------------------------------------------------
    # Overview
    # ------------------------------------------------------------------

    @router.get("/inspections")
    def get_inspections() -> list[dict[str, Any]]:
        if db is None:
            _not_configured()
        return _normalize_row(db, db.get_inspections())

    @router.get("/summary")
    def summary(
        inspection_id: Annotated[str | None, Query()] = None,
        top_n: int = 5,
    ) -> dict[str, Any]:
        if db is None:
            _not_configured()
        iid = _resolve_inspection(db, inspection_id)
        return _normalize_row(db, db.get_summary(inspection_id=iid, top_n=top_n))

    @router.get("/categories")
    def categories() -> list[dict[str, Any]]:
        if db is None:
            _not_configured()
        return [{"category": c["category"], "count": c["count"]} for c in db._category_counts()]

    @router.get("/detection-counts")
    def detection_counts(inspection_id: Annotated[str | None, Query()] = None) -> list[dict[str, Any]]:
        if db is None:
            _not_configured()
        iid = _resolve_inspection(db, inspection_id)
        return db.get_detection_counts_by_category(inspection_id=iid)

    @router.get("/temporal-clusters")
    def temporal_clusters(
        window_ms: int = 500,
        top_n: int = 10,
        inspection_id: Annotated[str | None, Query()] = None,
    ) -> list[dict[str, Any]]:
        if db is None:
            _not_configured()
        iid = _resolve_inspection(db, inspection_id)
        return _normalize_row(db, db.get_temporal_clusters(window_ms=window_ms, top_n=top_n, inspection_id=iid))

    @router.get("/recent-objects")
    def recent_objects(
        limit: Annotated[str | None, Query()] = None,
        inspection_id: Annotated[str | None, Query()] = None,
    ) -> list[dict[str, Any]]:
        if db is None:
            _not_configured()
        iid = _resolve_inspection(db, inspection_id)
        return _normalize_row(db, db.get_recent_objects(limit=limit, inspection_id=iid))

    @router.get("/top-objects")
    def top_objects(
        n: Annotated[str | None, Query()] = None,
        inspection_id: Annotated[str | None, Query()] = None,
    ) -> list[dict[str, Any]]:
        if db is None:
            _not_configured()
        iid = _resolve_inspection(db, inspection_id)
        return _normalize_row(db, db.get_top_objects(n=n, inspection_id=iid))

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    @router.get("/category/{name}/objects")
    def category_objects(
        name: str,
        limit: Annotated[str | None, Query()] = None,
        inspection_id: Annotated[str | None, Query()] = None,
    ) -> list[dict[str, Any]]:
        if db is None:
            _not_configured()
        iid = _resolve_inspection(db, inspection_id)
        cat = db._canonical_category(name)
        return _normalize_row(db, db.get_category_objects_with_images(cat, limit=limit, inspection_id=iid))

    @router.get("/category/{name}/coordinates")
    def category_coordinates(
        name: str,
        inspection_id: Annotated[str | None, Query()] = None,
    ) -> list[dict[str, Any]]:
        if db is None:
            _not_configured()
        iid = _resolve_inspection(db, inspection_id)
        cat = db._canonical_category(name)
        return _normalize_row(db, db.get_category_objects_with_coordinates(cat, inspection_id=iid))

    @router.get("/category/{name}/images")
    def category_images(
        name: str,
        limit: Annotated[str | None, Query()] = None,
        inspection_id: Annotated[str | None, Query()] = None,
    ) -> list[str]:
        if db is None:
            _not_configured()
        iid = _resolve_inspection(db, inspection_id)
        cat = db._canonical_category(name)
        return db.get_category_sample_images(cat, limit=limit, inspection_id=iid)

    @router.get("/category/{name}/timeline")
    def category_timeline(
        name: str,
        bucket_seconds: int = 60,
        inspection_id: Annotated[str | None, Query()] = None,
    ) -> list[dict[str, Any]]:
        if db is None:
            _not_configured()
        iid = _resolve_inspection(db, inspection_id)
        cat = db._canonical_category(name)
        return _normalize_row(db, db.get_category_detection_timeline(cat, bucket_seconds=bucket_seconds, inspection_id=iid))

    @router.get("/category/{name}/windows")
    def category_windows(
        name: str,
        inspection_id: Annotated[str | None, Query()] = None,
    ) -> list[dict[str, Any]]:
        if db is None:
            _not_configured()
        iid = _resolve_inspection(db, inspection_id)
        return _normalize_row(db, db.get_category_windows([name], inspection_id=iid))

    @router.get("/category/{name}/extent")
    def category_extent(
        name: str,
        inspection_id: Annotated[str | None, Query()] = None,
    ) -> dict[str, Any]:
        if db is None:
            _not_configured()
        iid = _resolve_inspection(db, inspection_id)
        cat = db._canonical_category(name)
        result = db.get_category_bounding_box(cat, inspection_id=iid)
        if result is None:
            return {"count": 0}
        return result

    # ------------------------------------------------------------------
    # Objects
    # ------------------------------------------------------------------

    @router.get("/objects/{object_id}")
    def object_detail(object_id: int) -> dict[str, Any]:
        if db is None:
            _not_configured()
        result = db.get_object_by_id(object_id)
        if result is None:
            raise HTTPException(status_code=404, detail=f"Object {object_id} not found")
        return _normalize_row(db, result)

    @router.get("/objects/{object_id}/frames")
    def object_frames(object_id: int) -> list[str]:
        if db is None:
            _not_configured()
        return db.get_object_image_paths(object_id)

    @router.get("/objects/{object_id}/timeline")
    def object_timeline(object_id: int) -> list[dict[str, Any]]:
        if db is None:
            _not_configured()
        return _normalize_row(db, db.get_object_timeline(object_id))

    @router.get("/objects/{object_id}/movement")
    def object_movement(object_id: int) -> list[dict[str, Any]]:
        if db is None:
            _not_configured()
        return _normalize_row(db, db.get_object_movement(object_id))

    @router.get("/objects/{object_id}/nearby")
    def object_nearby(
        object_id: int,
        radius_m: float = 2.0,
        inspection_id: Annotated[str | None, Query()] = None,
    ) -> list[dict[str, Any]]:
        if db is None:
            _not_configured()
        iid = _resolve_inspection(db, inspection_id)
        return _normalize_row(db, db.get_nearest_objects_to_object(object_id, radius_m=radius_m, inspection_id=iid))

    @router.get("/objects/distance")
    def object_distance(a: int = Query(...), b: int = Query(...)) -> dict[str, Any]:
        if db is None:
            _not_configured()
        result = db.get_object_distance(a, b)
        if result is None:
            raise HTTPException(status_code=404, detail="Could not find both objects")
        return result

    # ------------------------------------------------------------------
    # Time range
    # ------------------------------------------------------------------

    @router.get("/time-range/objects")
    def time_range_objects(
        start: str = Query(..., description="ISO / clock time / ns"),
        end: str = Query(..., description="ISO / clock time / ns"),
        limit: Annotated[str | None, Query()] = None,
        inspection_id: Annotated[str | None, Query()] = None,
    ) -> list[dict[str, Any]]:
        if db is None:
            _not_configured()
        iid = _resolve_inspection(db, inspection_id)
        return _normalize_row(db, db.get_objects_in_time_range(start, end, limit=limit, inspection_id=iid))

    @router.get("/time-range/detections")
    def time_range_detections(
        start: str = Query(...),
        end: str = Query(...),
        limit: Annotated[str | None, Query()] = None,
        inspection_id: Annotated[str | None, Query()] = None,
    ) -> list[dict[str, Any]]:
        if db is None:
            _not_configured()
        iid = _resolve_inspection(db, inspection_id)
        return _normalize_row(db, db.get_detections_in_time_range(start, end, limit=limit, inspection_id=iid))

    @router.get("/time-range/images")
    def time_range_images(
        start: str = Query(...),
        end: str = Query(...),
        category: str | None = None,
        limit: Annotated[str | None, Query()] = None,
        inspection_id: Annotated[str | None, Query()] = None,
    ) -> list[str]:
        if db is None:
            _not_configured()
        iid = _resolve_inspection(db, inspection_id)
        cat = db._canonical_category(category) if category else None
        return db.get_images_in_time_range(start, end, category=cat, limit=limit, inspection_id=iid)

    @router.get("/time-range/category/{name}")
    def time_range_category(
        name: str,
        start: str = Query(...),
        end: str = Query(...),
        limit: Annotated[str | None, Query()] = None,
        inspection_id: Annotated[str | None, Query()] = None,
    ) -> list[dict[str, Any]]:
        if db is None:
            _not_configured()
        iid = _resolve_inspection(db, inspection_id)
        cat = db._canonical_category(name)
        return _normalize_row(db, db.get_objects_by_category_in_time_range(cat, start, end, limit=limit, inspection_id=iid))

    @router.get("/temporal-cluster")
    def temporal_cluster(
        center_time: str = Query(...),
        window_ms: int = 500,
        limit: Annotated[str | None, Query()] = None,
        inspection_id: Annotated[str | None, Query()] = None,
    ) -> dict[str, Any]:
        if db is None:
            _not_configured()
        iid = _resolve_inspection(db, inspection_id)
        return _normalize_row(db, db.get_objects_in_temporal_cluster(center_time, window_ms=window_ms, limit=limit, inspection_id=iid))

    # ------------------------------------------------------------------
    # Proximity
    # ------------------------------------------------------------------

    @router.get("/proximity")
    def proximity(
        target: str = Query(...),
        others: str = Query("", description="comma-separated categories"),
        radius_m: float = 2.0,
        with_images: bool = False,
        limit: Annotated[str | None, Query()] = None,
        nearby_limit: Annotated[str | None, Query()] = None,
        inspection_id: Annotated[str | None, Query()] = None,
    ) -> dict[str, Any]:
        if db is None:
            _not_configured()
        iid = _resolve_inspection(db, inspection_id)
        target_cat = db._canonical_category(target)
        other_list = [c.strip() for c in others.split(",") if c.strip()]
        other_cats = [db._canonical_category(c) for c in other_list]
        if with_images:
            rows = db.get_category_proximity_with_images(
                target_cat, other_cats, radius_m, limit=limit, nearby_limit=nearby_limit, inspection_id=iid
            )
        else:
            rows = db.get_category_proximity(target_cat, other_cats, radius_m, inspection_id=iid)
        return {"target_category": target_cat, "others": other_cats, "radius_m": radius_m, "results": _normalize_row(db, rows)}

    @router.get("/proximity/objects")
    def proximity_objects(
        object_ids: str = Query(..., description="comma-separated object ids"),
        target: str = Query(...),
        radius_m: float = 2.0,
        limit: Annotated[str | None, Query()] = None,
        nearby_limit: Annotated[str | None, Query()] = None,
        inspection_id: Annotated[str | None, Query()] = None,
    ) -> dict[str, Any]:
        if db is None:
            _not_configured()
        iid = _resolve_inspection(db, inspection_id)
        ids = [int(x) for x in object_ids.split(",") if x.strip().lstrip("-").isdigit()]
        if not ids:
            raise HTTPException(status_code=400, detail="No valid object ids provided")
        target_cat = db._canonical_category(target)
        rows = db.get_objects_proximity_with_images(
            ids, target_cat, radius_m, limit=limit, nearby_limit=nearby_limit, inspection_id=iid
        )
        return {"target_category": target_cat, "object_ids": ids, "radius_m": radius_m, "results": _normalize_row(db, rows)}

    @router.get("/near-position")
    def near_position(
        x: float = Query(...),
        y: float = Query(...),
        z: float = Query(...),
        radius_m: float = 2.0,
        category: str | None = None,
        inspection_id: Annotated[str | None, Query()] = None,
    ) -> list[dict[str, Any]]:
        if db is None:
            _not_configured()
        iid = _resolve_inspection(db, inspection_id)
        cat = db._canonical_category(category) if category else None
        return _normalize_row(db, db.get_objects_near_position(x, y, z, radius_m, category=cat, inspection_id=iid))

    @router.get("/cooccurrence")
    def cooccurrence(window_ms: int = 500, top_n: int = 10, inspection_id: Annotated[str | None, Query()] = None) -> list[dict[str, Any]]:
        if db is None:
            _not_configured()
        iid = _resolve_inspection(db, inspection_id)
        return db.get_category_cooccurrence(window_ms=window_ms, top_n=top_n, inspection_id=iid)

    # ------------------------------------------------------------------
    # Anomalies
    # ------------------------------------------------------------------

    @router.get("/anomalies/types")
    def anomaly_types() -> list[str]:
        if db is None:
            _not_configured()
        return db.get_anomaly_types()

    @router.get("/anomalies/summary")
    def anomaly_summary(inspection_id: Annotated[str | None, Query()] = None) -> dict[str, Any]:
        if db is None:
            _not_configured()
        iid = _resolve_inspection(db, inspection_id)
        return _normalize_row(db, db.get_anomaly_summary(inspection_id=iid))

    @router.get("/anomalies")
    def anomalies(
        anomaly_id: Annotated[str | None, Query()] = None,
        anomaly_type: str | None = None,
        limit: Annotated[str | None, Query()] = None,
        inspection_id: Annotated[str | None, Query()] = None,
    ) -> list[dict[str, Any]]:
        if db is None:
            _not_configured()
        iid = _resolve_inspection(db, inspection_id)
        aid = int(anomaly_id) if anomaly_id and str(anomaly_id).lstrip("-").isdigit() else None
        rows = db.get_anomalies(anomaly_id=aid, anomaly_type=anomaly_type, inspection_id=iid, limit=limit)
        return _normalize_row(db, rows)

    @router.get("/anomalies/locations")
    def anomaly_locations(inspection_id: Annotated[str | None, Query()] = None) -> list[dict[str, Any]]:
        if db is None:
            _not_configured()
        iid = _resolve_inspection(db, inspection_id)
        return _normalize_row(db, db.get_anomaly_locations(inspection_id=iid))

    # ------------------------------------------------------------------
    # Image frame ↔ objects
    # ------------------------------------------------------------------

    @router.get("/images/{filename}/objects")
    def objects_in_image(filename: str) -> list[dict[str, Any]]:
        if db is None:
            _not_configured()
        return _normalize_row(db, db.get_objects_in_image(filename))

    @router.get("/search")
    def search(q: str = Query(..., description="object id, category, or frame name")) -> dict[str, Any]:
        """Quick lookup: numeric => object id; a category name => category objects; else frame."""
        if db is None:
            _not_configured()
        q = q.strip()
        if not q:
            return {"type": "empty"}
        if q.lstrip("-").isdigit():
            oid = int(q)
            obj = db.get_object_by_id(oid)
            return {"type": "object", "object_id": oid, "found": obj is not None,
                    "object": _normalize_row(db, obj) if obj else None}
        if q.lower() in {c.lower() for c in db.get_categories()}:
            cat = db._canonical_category(q)
            return {"type": "category", "category": cat,
                    "objects": _normalize_row(db, db.get_category_objects_with_images(cat, limit="all"))}
        objects = db.get_objects_in_image(q)
        return {"type": "frame", "filename": q, "objects": _normalize_row(db, objects)}

    # ------------------------------------------------------------------
    # Rerun viewer
    # ------------------------------------------------------------------

    @router.post("/rerun/highlight")
    def rerun_highlight(
        payload: dict[str, Any] = Body(
            ...,
            example={
                "object_ids": [9, 11],
                "categories": ["Lights"],
                "coordinates": [{"x": 0.0, "y": 1.0, "z": 2.0, "label": "Object 9: Lights"}],
                "inspection_id": None,
                "label": "selection",
                "keep_existing": False,
            },
        ),
    ) -> dict[str, Any]:
        if db is None:
            _not_configured()
        status = db.push_highlight(payload)
        return {"status": status, "rerun": _rerun_status_dict()}

    @router.post("/rerun/clear")
    def rerun_clear() -> dict[str, Any]:
        if rerun is None:
            return {"status": "Rerun visualization is not configured.", "rerun": _rerun_status_dict()}
        status = rerun.clear()
        return {"status": status, "rerun": _rerun_status_dict()}

    @router.post("/rerun/plot-path")
    def rerun_plot_path(
        payload: dict[str, Any] = Body(
            ...,
            example={
                "waypoints": [
                    {"x": 0.0, "y": 0.0, "z": 0.0, "label": "WP1"},
                    {"x": 1.0, "y": 0.0, "z": 0.0, "label": "WP2"},
                ],
                "label": "inspection_path",
            },
        ),
    ) -> dict[str, Any]:
        if rerun is None:
            return {"status": "Rerun visualization is not configured.", "rerun": _rerun_status_dict()}
        waypoints = payload.get("waypoints") or []
        label = payload.get("label")
        status = rerun.plot_path(waypoints, label=label)
        return {"status": status, "rerun": _rerun_status_dict()}

    # ------------------------------------------------------------------
    # Annotation (reuses the tool's vision path on an image URL)
    # ------------------------------------------------------------------

    @router.post("/annotate-image-url")
    async def annotate_image_url(
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        if db is None or db.vision_annotator is None:
            raise HTTPException(status_code=503, detail="Vision annotator not configured.")
        image_url = payload.get("image_url")
        object_id = payload.get("object_id")
        category = payload.get("category")
        question = payload.get("question")
        if not image_url and object_id is None and not category:
            raise HTTPException(status_code=400, detail="Provide image_url, object_id, or category.")
        try:
            result = await db.annotate_image(
                image_url=image_url,
                object_id=int(object_id) if object_id is not None else None,
                category=db._canonical_category(category) if category else None,
                question=question,
                limit=payload.get("limit", 1),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("annotate-image-url failed: %s", exc)
            raise HTTPException(status_code=500, detail=f"Annotation failed: {exc}") from exc
        return result

    # ------------------------------------------------------------------
    # Export (CSV)
    # ------------------------------------------------------------------

    @router.post("/export")
    def export(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Serialize a named worker query to CSV for the console's export buttons.

        body: {query: <name>, args: {...}}  — args mirror the worker query args.
        """
        if db is None:
            _not_configured()
        query = payload.get("query")
        args: dict[str, Any] = payload.get("args") or {}
        iid = _resolve_inspection(db, args.get("inspection_id"))

        if query == "category_objects":
            rows = db.get_category_objects_with_images(
                db._canonical_category(args.get("category", "")), limit="all", inspection_id=iid
            )
        elif query == "anomalies":
            rows = db.get_anomalies(anomaly_type=args.get("anomaly_type"), inspection_id=iid, limit="all")
        elif query == "objects_in_time_range":
            rows = db.get_objects_in_time_range(args.get("start", ""), args.get("end", ""), limit="all", inspection_id=iid)
        elif query == "detections_in_time_range":
            rows = db.get_detections_in_time_range(args.get("start", ""), args.get("end", ""), limit="all", inspection_id=iid)
        elif query == "summary":
            summary = db.get_summary(inspection_id=iid)
            rows = [{"metric": "total_objects", "value": summary["total_objects"]}] + [
                {"metric": c["category"], "value": c["count"]} for c in summary["categories"]
            ]
        elif query == "top_objects":
            rows = db.get_top_objects(n="all", inspection_id=iid)
        else:
            raise HTTPException(status_code=400, detail=f"Unknown export query: {query}")

        return {
            "filename": f"{query}.csv",
            "csv": _to_csv_rows(rows),
            "row_count": len(rows),
        }

    return router