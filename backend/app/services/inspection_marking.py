"""Inspection marking API + Rerun visualizer (backend service, no ROS/open3d).

Adapted from ``MTR Inspection Database/inspection_marking.py`` so the backend
rerun visualizer can load the photo-colored global map and per-object segmented
clouds directly from the inspection database output folder.

The map and every object cloud are in the camera_init frame, so marks land on
the map with no extra transform. A visualization-only leveling rotation (default
[0, 20, 0] deg) is logged at world/leveled; the map and marks are logged under
it so they render flat.
"""

from __future__ import annotations

import math
import re
import sqlite3
import zlib
from pathlib import Path
from typing import Any

import numpy as np
import rerun as rr
import rerun.blueprint as rrb

DEFAULT_DB = "../MTR Inspection Database/inspection_v2_mtr_new.db"
DEFAULT_MAP = "../MTR Inspection Database/outputs/colored_map.pcd"
DEFAULT_OBJECTS_DIR = "../MTR Inspection Database/outputs/objects"
DEFAULT_LEVELLING_RPY_DEG = [0.0, 20.0, 0.0]

WORLD = "world"

# Category palette + slug (inlined from utils/common_utils so this script has no
# ROS dependency; kept in sync with color_for_category / category_slug there).
_CATEGORY_SLUGS = {
    "Advertisement Board": "adboards",
    "Exit Sign": "exit_sign",
    "Lights": "lights",
    "Map": "map",
    "TV": "tv",
    "Ticket Gate": "ticket_gate",
    "Poster": "poster",
}

_CATEGORY_COLORS_RGB = {
    "Advertisement Board": (231, 76, 60),  # red
    "Exit Sign": (52, 152, 219),  # blue
    "Lights": (241, 196, 15),  # yellow
    "Map": (155, 89, 182),  # purple
    "TV": (26, 188, 156),  # teal
    "Ticket Gate": (230, 126, 34),  # orange
    "Poster": (236, 64, 122),  # pink
}


def _hsv_to_rgb(h: float, s: float, v: float) -> tuple[float, float, float]:
    i = int(h * 6.0)
    f = h * 6.0 - i
    p = v * (1.0 - s)
    q = v * (1.0 - s * f)
    t = v * (1.0 - s * (1.0 - f))
    i %= 6
    if i == 0:
        r, g, b = v, t, p
    elif i == 1:
        r, g, b = q, v, p
    elif i == 2:
        r, g, b = p, v, t
    elif i == 3:
        r, g, b = p, q, v
    elif i == 4:
        r, g, b = t, p, v
    else:
        r, g, b = v, p, q
    return r, g, b


def color_for_category(category: Any, alpha: float = 1.0) -> list[int]:
    """Stable RGBA uint8 [r, g, b, a] for a category name (mirrors common_utils)."""
    rgb = _CATEGORY_COLORS_RGB.get(category)
    if rgb is None:
        key = str(category) if category is not None else "unknown"
        h = (zlib.crc32(key.encode("utf-8")) * 0.6180339887498949) % 1.0
        r, g, b = _hsv_to_rgb(h, 0.80, 0.95)
    else:
        r, g, b = (c / 255.0 for c in rgb)
    return [
        int(round(r * 255)),
        int(round(g * 255)),
        int(round(b * 255)),
        int(round(alpha * 255)),
    ]


def category_slug(category: Any) -> str:
    """Path-safe slug for a category name (mirrors common_utils)."""
    if category is None:
        return "unknown"
    if category in _CATEGORY_SLUGS:
        return _CATEGORY_SLUGS[category]
    slug = re.sub(r"[^a-z0-9]+", "_", str(category).lower()).strip("_")
    return slug or "unknown"


def rpy_deg_to_quaternion(roll_deg: float, pitch_deg: float, yaw_deg: float) -> list[float]:
    """Roll/pitch/yaw in degrees -> quaternion [x, y, z, w]."""
    r = math.radians(roll_deg)
    p = math.radians(pitch_deg)
    y = math.radians(yaw_deg)
    cr, sr = math.cos(r / 2), math.sin(r / 2)
    cp, sp = math.cos(p / 2), math.sin(p / 2)
    cy, sy = math.cos(y / 2), math.sin(y / 2)
    return [
        sr * cp * cy - cr * sp * sy,  # x
        cr * sp * cy + sr * cp * sy,  # y
        cr * cp * sy - sr * cp * cy,  # z
        cr * cp * cy + sr * sp * sy,  # w
    ]


def _load_pcd(path: str | Path) -> tuple[np.ndarray, np.ndarray | None]:
    """Minimal pure-numpy PCD reader.

    Supports the two PCD variants used by this project:
    * ASCII, fields ``x y z`` (per-object segmented clouds).
    * binary, fields ``x y z rgb`` with all fields ``SIZE 4 TYPE F`` (colored_map.pcd).

    Returns ``(points, colors)`` where ``points`` is ``(N, 3) float32`` and
    ``colors`` is ``(N, 4) uint8`` RGBA or ``None`` when the file has no rgb field.

    For binary rgb, the 4th float column is viewed as bytes in BGRA order (PCL's
    little-endian packing) and reordered to RGBA with alpha forced to 255.
    """
    path = Path(path)
    with open(path, "rb") as f:
        raw = f.read()

    data_header = b"\nDATA "
    header_end = raw.find(data_header)
    if header_end < 0:
        raise ValueError(f"Invalid PCD file (no DATA line): {path}")

    header = raw[:header_end].decode("ascii", errors="ignore")
    data_line_start = header_end + 1
    data_line_end = raw.find(b"\n", data_line_start)
    if data_line_end < 0:
        data_line_end = len(raw)
    data_line = raw[data_line_start:data_line_end].decode("ascii").strip()
    data_type = data_line.split()[1].lower()
    data_start = data_line_end + 1

    fields: list[str] = []
    sizes: list[int] = []
    types: list[str] = []
    counts: list[int] = []
    width = height = points_count = None

    for line in header.splitlines():
        parts = line.strip().split()
        if not parts or parts[0].startswith("#"):
            continue
        key = parts[0].upper()
        if key == "FIELDS":
            fields = parts[1:]
        elif key == "SIZE":
            sizes = [int(x) for x in parts[1:]]
        elif key == "TYPE":
            types = parts[1:]
        elif key == "COUNT":
            counts = [int(x) for x in parts[1:]]
        elif key == "WIDTH":
            width = int(parts[1])
        elif key == "HEIGHT":
            height = int(parts[1])
        elif key == "POINTS":
            points_count = int(parts[1])

    n_fields = len(fields)
    if not counts:
        counts = [1] * n_fields

    expected = points_count
    if expected is None and width is not None and height is not None:
        expected = width * height

    if data_type == "ascii":
        pts: list[list[float]] = []
        for line in raw[data_start:].decode("ascii", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            vals = line.split()
            if len(vals) < n_fields:
                continue
            pts.append([float(v) for v in vals[:n_fields]])
        points = np.asarray(pts, dtype=np.float32)
        return points, None

    if data_type == "binary":
        np_types = {
            ("F", 4): np.float32,
            ("F", 8): np.float64,
            ("U", 1): np.uint8,
            ("U", 2): np.uint16,
            ("U", 4): np.uint32,
            ("U", 8): np.uint64,
            ("I", 1): np.int8,
            ("I", 2): np.int16,
            ("I", 4): np.int32,
            ("I", 8): np.int64,
        }
        dt: list[tuple[str, type[np.generic], int]] = []
        for field, size, typ, count in zip(fields, sizes, types, counts):
            base = np_types.get((typ.upper(), size))
            if base is None:
                raise ValueError(f"Unsupported PCD field type {typ}{size} for {field}")
            dt.append((field, base, count))

        count = -1 if expected is None else expected
        arr = np.fromfile(path, dtype=np.dtype(dt), offset=data_start, count=count)

        def _flat(field_name: str) -> np.ndarray:
            """Return a 1-D view of a single-count structured field."""
            col = arr[field_name]
            if col.ndim > 1:
                col = col.reshape(-1)
            return col

        points = np.empty((arr.shape[0], 3), dtype=np.float32)
        points[:, 0] = _flat("x")
        points[:, 1] = _flat("y")
        points[:, 2] = _flat("z")

        colors = None
        if "rgb" in fields:
            rgb_floats = np.ascontiguousarray(_flat("rgb"))
            rgb_packed = rgb_floats.view(np.uint8).reshape(-1, 4)  # PCL packs BGRA
            colors = np.empty((rgb_packed.shape[0], 4), dtype=np.uint8)
            colors[:, 0] = rgb_packed[:, 2]  # R
            colors[:, 1] = rgb_packed[:, 1]  # G
            colors[:, 2] = rgb_packed[:, 0]  # B
            colors[:, 3] = 255  # A
        return points, colors

    raise ValueError(f"Unsupported PCD DATA type '{data_type}' in {path}")


def _voxel_downsample(
    pts: np.ndarray,
    colors: np.ndarray | None,
    voxel: float,
) -> tuple[np.ndarray, np.ndarray | None]:
    """Simple numpy voxel downsampling."""
    if voxel <= 0.0 or pts.shape[0] == 0:
        return pts, colors
    coords = np.floor(pts / voxel).astype(np.int64)
    uniq, inv = np.unique(coords, axis=0, return_inverse=True)
    down_pts = np.zeros((uniq.shape[0], 3), dtype=np.float32)
    np.add.at(down_pts, inv, pts)
    counts = np.bincount(inv, minlength=uniq.shape[0]).reshape(-1, 1)
    down_pts /= np.maximum(counts, 1)

    down_colors = None
    if colors is not None:
        acc = np.zeros((uniq.shape[0], 4), dtype=np.uint32)
        np.add.at(acc, inv, colors.astype(np.uint32))
        down_colors = (acc / np.maximum(counts, 1)).astype(np.uint8)
    return down_pts, down_colors


class InspectionMarker:
    """Load the colored global map into Rerun and expose mark_by_id /
    mark_by_category / clean_markings against the inspection DB."""

    def __init__(
        self,
        db_path: str | Path = DEFAULT_DB,
        map_path: str | Path | None = DEFAULT_MAP,
        objects_dir: str | Path = DEFAULT_OBJECTS_DIR,
        voxel: float = 0.1,
        spawn_viewer: bool = True,
        connect_url: str | None = None,
        rrd_path: str | Path | None = None,
        load_map: bool = True,
        leveling_rpy_deg: list[float] | tuple[float, ...] | None = None,
        app_id: str = "inspection_marking",
    ):
        self.db_path = Path(db_path)
        self.map_path = Path(map_path) if map_path else None
        self.objects_dir = Path(objects_dir)
        self.voxel = voxel
        self.leveling_rpy_deg = (
            list(leveling_rpy_deg)
            if leveling_rpy_deg is not None
            else list(DEFAULT_LEVELLING_RPY_DEG)
        )
        self.app_id = app_id

        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row

        self._init_rerun(spawn_viewer, connect_url, rrd_path)
        if load_map and self.map_path:
            self._load_map()

    # ------------------------------------------------------------------
    # Rerun setup
    # ------------------------------------------------------------------

    def _init_rerun(
        self,
        spawn_viewer: bool,
        connect_url: str | None,
        rrd_path: str | Path | None,
    ) -> None:
        """Init the recording, attach sink(s), log the world frame + blueprint."""
        rr.init(self.app_id)

        sinks: list[Any] = []
        if spawn_viewer:
            rr.spawn(connect=False)
            sinks.append(rr.GrpcSink())
        elif connect_url is not None:
            sinks.append(rr.GrpcSink(connect_url) if connect_url else rr.GrpcSink())
        if rrd_path:
            rrd_path = str(Path(rrd_path).expanduser())
            Path(rrd_path).parent.mkdir(parents=True, exist_ok=True)
            sinks.append(rr.FileSink(rrd_path))
            print(f"[rerun] recording to .rrd: {rrd_path}")
        if sinks:
            rr.set_sinks(*sinks)
            if spawn_viewer:
                print("[rerun] viewer spawned (streaming via gRPC)")
        else:
            print(
                "[rerun] WARNING: no sink - nothing will be visualized "
                "(use spawn=True / connect_url / rrd_path)"
            )

        rr.log(WORLD, rr.ViewCoordinates.RIGHT_HAND_Z_UP, static=True)

        if any(abs(a) > 1e-6 for a in self.leveling_rpy_deg):
            q = rpy_deg_to_quaternion(*self.leveling_rpy_deg)
            rr.log(
                f"{WORLD}/leveled",
                rr.Transform3D(rotation=rr.Quaternion(xyzw=q)),
                static=True,
            )
            base = f"{WORLD}/leveled"
            print(f"[rerun] leveling applied: RPY={self.leveling_rpy_deg} deg")
        else:
            base = WORLD
        self.base_path = base
        rr.log(f"{base}/camera_init", rr.Transform3D(translation=[0, 0, 0]), static=True)
        self.map_ent = f"{base}/camera_init/colored_map"
        self.marks_root = f"{base}/camera_init/marks"

        bp = rrb.Blueprint(
            rrb.Spatial3DView(
                origin=WORLD, name="Inspection map", contents="$origin/**"
            ),
            auto_layout=False,
            auto_views=False,
        )
        rr.send_blueprint(bp, make_active=True, make_default=True)

    def _load_map(self) -> None:
        """Load the photo-colored map PCD (xyz + rgb) and log it static."""
        if not self.map_path or not self.map_path.exists():
            print(
                f"[map] WARNING: {self.map_path} not found - continuing "
                "without a map (marks only)."
            )
            return
        print(f"[map] loading {self.map_path} ...")
        pts, colors = _load_pcd(self.map_path)
        if pts.shape[0] == 0:
            print("[map] WARNING: empty cloud - continuing without a map.")
            return
        if self.voxel and self.voxel > 0.0:
            pts, colors = _voxel_downsample(pts, colors, float(self.voxel))
        if colors is not None:
            rr.log(self.map_ent, rr.Points3D(pts, colors=colors), static=True)
        else:
            rr.log(self.map_ent, rr.Points3D(pts), static=True)
        print(f"[map] logged {pts.shape[0]} pts ({self.map_ent})")

    # ------------------------------------------------------------------
    # DB queries
    # ------------------------------------------------------------------

    def _query_object(self, object_id: int) -> dict[str, Any] | None:
        cur = self.conn.execute(
            "SELECT o.id, c.name AS category, "
            "o.centroid_x, o.centroid_y, o.centroid_z FROM objects o "
            "LEFT JOIN categories c ON o.category_id = c.id WHERE o.id = ?",
            (object_id,),
        )
        row = cur.fetchone()
        return dict(row) if row is not None else None

    def _query_objects_by_category(
        self, category: str, inspection_id: int | None = None
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Return (matched_name, rows). category may be exact name,
        case-insensitive name, or slug. rows ordered by id. When
        ``inspection_id`` is given, only objects seen in that inspection
        (via detections -> images) are returned.

        Each row includes the object's centroid and the inspection it belongs to
        (derived from its detections), so missing segmented clouds can fall back
        to a centroid point and unscoped marks can be grouped per inspection.
        """
        cats = self._categories()
        wanted = category.strip()
        wanted_slug = category_slug(wanted)
        match = None
        for _cid, name in cats:
            if (
                name == wanted
                or name.lower() == wanted.lower()
                or category_slug(name) == wanted_slug
            ):
                match = name
                break
        if match is None:
            return None, []
        inspection_select = (
            "(SELECT MIN(i.inspection_id) FROM detections d JOIN images i ON i.id=d.image_id "
            "WHERE d.object_id=o.id) AS inspection_id"
        )
        if inspection_id is not None:
            cur = self.conn.execute(
                "SELECT DISTINCT o.id, c.name AS category, "
                "o.centroid_x, o.centroid_y, o.centroid_z, "
                f"{inspection_select} "
                "FROM objects o "
                "JOIN categories c ON o.category_id = c.id "
                "JOIN detections d ON d.object_id = o.id "
                "JOIN images i ON i.id = d.image_id "
                "WHERE c.name = ? AND i.inspection_id = ? "
                "ORDER BY o.id",
                (match, int(inspection_id)),
            )
        else:
            cur = self.conn.execute(
                "SELECT o.id, c.name AS category, "
                "o.centroid_x, o.centroid_y, o.centroid_z, "
                f"{inspection_select} "
                "FROM objects o "
                "JOIN categories c ON o.category_id = c.id WHERE c.name = ? "
                "ORDER BY o.id",
                (match,),
            )
        return match, [dict(r) for r in cur.fetchall()]

    def _load_object_cloud(self, object_id: int) -> np.ndarray | None:
        """Read objects/<id>.pcd -> (N,3) float32, or None if absent/empty."""
        path = self.objects_dir / f"{int(object_id)}.pcd"
        if not path.exists():
            return None
        pts, _ = _load_pcd(path)
        return pts if pts.shape[0] > 0 else None

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    def mark_by_id(self, object_id: int) -> None:
        """Mark one object's segmented point cloud (or centroid fallback) on the map."""
        row = self._query_object(object_id)
        if row is None:
            print(
                f"[mark_by_id] no object with id={object_id} "
                f"(ids {self._min_object_id()}..{self._max_object_id()})"
            )
            return
        pts = self._load_object_cloud(object_id)
        fallback = False
        if pts is None:
            cx, cy, cz = row.get("centroid_x"), row.get("centroid_y"), row.get("centroid_z")
            if cx is None or cy is None or cz is None:
                print(
                    f"[mark_by_id] id={object_id}: no segmented cloud "
                    f"({self.objects_dir}/{object_id}.pcd) and no centroid"
                )
                return
            pts = np.array([[float(cx), float(cy), float(cz)]], dtype=np.float32)
            fallback = True
        category = row["category"] or "unknown"
        color = np.array(color_for_category(category), dtype=np.uint8)
        rr.log(
            f"{self.marks_root}/id_{int(object_id)}",
            rr.Points3D(
                pts,
                colors=np.tile(color, (pts.shape[0], 1)),
            ),
        )
        fb = " (centroid fallback)" if fallback else ""
        print(f"[mark_by_id] id={object_id} {category}: {pts.shape[0]} pts{fb}")

    def mark_by_ids(self, object_ids: list[int]) -> None:
        """Mark a list of object ids on the map."""
        for object_id in object_ids:
            self.mark_by_id(object_id)

    def mark_by_category(self, category: str, inspection_id: int | None = None) -> None:
        """Mark every object of a category on the map.

        When ``inspection_id`` is given, only that inspection's objects are
        marked. When ``inspection_id`` is None (the default), objects from ALL
        inspections are marked, grouped under per-inspection entity paths so the
        viewer can distinguish which inspection each object belongs to.

        Segmented point clouds are used when available; otherwise the object's
        centroid is logged as a fallback point so nothing disappears from the
        viewer.
        """
        match, rows = self._query_objects_by_category(category, inspection_id=inspection_id)
        if match is None:
            print(
                f"[mark_by_category] unknown category '{category}'. "
                f"Available: {', '.join(n for _, n in self._categories())}"
            )
            return
        color = np.array(color_for_category(match), dtype=np.uint8)

        # Group by inspection so unscoped marks are still organized per inspection.
        from collections import defaultdict
        by_inspection: dict[int, list[tuple[int, np.ndarray]]] = defaultdict(list)
        fallback_ids: list[int] = []
        for r in rows:
            pts = self._load_object_cloud(r["id"])
            if pts is None:
                cx, cy, cz = r.get("centroid_x"), r.get("centroid_y"), r.get("centroid_z")
                if cx is not None and cy is not None and cz is not None:
                    pts = np.array([[float(cx), float(cy), float(cz)]], dtype=np.float32)
                    fallback_ids.append(int(r["id"]))
            if pts is None:
                continue
            insp = int(r["inspection_id"]) if r.get("inspection_id") is not None else 0
            by_inspection[insp].append((int(r["id"]), pts))

        if not by_inspection:
            scope = f" (inspection {inspection_id})" if inspection_id is not None else ""
            print(f"[mark_by_category] '{match}'{scope}: no clouds or centroids found")
            return

        total_pts = 0
        total_objs = 0
        for insp, pairs in sorted(by_inspection.items()):
            chunks = [pts for _, pts in pairs]
            ids = [oid for oid, _ in pairs]
            pts = np.concatenate(chunks)
            ent = f"{self.marks_root}/cat_{category_slug(match)}/insp_{insp}"
            rr.log(
                ent,
                rr.Points3D(
                    pts,
                    colors=np.tile(color, (pts.shape[0], 1)),
                ),
            )
            total_pts += pts.shape[0]
            total_objs += len(ids)
            print(
                f"[mark_by_category] '{match}' inspection {insp}: "
                f"{len(ids)} objects, {pts.shape[0]} pts (ids {ids[0]}..{ids[-1]})"
            )
        fb = f" ({len(fallback_ids)} centroid fallback)" if fallback_ids else ""
        scope = f" inspection {inspection_id}," if inspection_id is not None else " all inspections,"
        print(
            f"[mark_by_category] '{match}':{scope} {total_objs} objects, "
            f"{total_pts} pts{fb}"
        )

    def clean_markings(self) -> None:
        """Remove every mark from the viewer."""
        rr.log(self.marks_root, rr.Clear(recursive=True))
        print("[clean_markings] cleared all marks")

    def list_categories(self) -> None:
        """Print each category with its object count, plus the id range."""
        print("Categories (id -> name: objects):")
        total = 0
        for cid, name in self._categories():
            n = self.conn.execute(
                "SELECT COUNT(*) FROM objects WHERE category_id = ?", (cid,)
            ).fetchone()[0]
            print(f"  {cid:>2} -> {name:<22} {n}")
            total += n
        print(
            f"  total objects: {total}  (id range "
            f"{self._min_object_id()}..{self._max_object_id()})"
        )

    # ------------------------------------------------------------------
    # Small DB helpers
    # ------------------------------------------------------------------

    def _categories(self) -> list[tuple[int, str]]:
        cur = self.conn.execute("SELECT id, name FROM categories ORDER BY id")
        return [(int(r["id"]), r["name"]) for r in cur.fetchall()]

    def _min_object_id(self) -> int:
        r = self.conn.execute("SELECT MIN(id) FROM objects").fetchone()
        return int(r[0]) if r[0] is not None else 0

    def _max_object_id(self) -> int:
        r = self.conn.execute("SELECT MAX(id) FROM objects").fetchone()
        return int(r[0]) if r[0] is not None else 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        try:
            rr.disconnect()
        except Exception:
            pass
        if self.conn:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> "InspectionMarker":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
