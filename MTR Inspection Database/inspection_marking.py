#!/usr/bin/env python3
"""
Inspection marking API + Rerun visualizer (standalone, no ROS).

Loads the photo-colored global map (saved by fusion_node to
data/outputs/colored_map.pcd) into a Rerun viewer and marks inspection objects
from inspection_v2.db on it as their segmented point clouds (the per-object PCDs
under data/outputs/objects/). The map and every object cloud are in the
camera_init frame, so marks land on the map with no extra transform.

Three API calls (class InspectionMarker):
    mark_by_id(object_id)       mark one object's segmented cloud
    mark_by_category(category)  mark every object's cloud of a category
    clean_markings()            remove all marks

The map is recorded by a tilted LiDAR, so a visualization-only leveling rotation
(default [0, 20, 0] deg, matching rerun_bridge params.yaml) is logged at
world/leveled; the map and marks are logged under it, so they inherit the
rotation and render flat. Point clouds have no orientation, so (unlike boxes)
they need no pre-rotation.

Usage:
    python3 inspection_marking.py
    > list
    > id 9
    > category Lights
    > clean
    > quit
"""

import argparse
import math
import re
import sqlite3
import sys
import zlib
from pathlib import Path

import numpy as np
import open3d as o3d
import rerun as rr
import rerun.blueprint as rrb

DEFAULT_DB = "/home/robot/fastlio_ws/data/inspection_v2.db"
DEFAULT_MAP = "/home/robot/fastlio_ws/data/outputs/colored_map.pcd"
DEFAULT_OBJECTS_DIR = "/home/robot/fastlio_ws/data/outputs/objects"
DEFAULT_LEVELLING_RPY_DEG = [0.0, 20.0, 0.0]

WORLD = "world"


# Category palette + slug (inlined from utils/common_utils so this script has no
# ROS dependency; kept in sync with color_for_category / category_slug there).
_CATEGORY_SLUGS = {
    'Advertisement Board': 'adboards',
    'Exit Sign': 'exit_sign',
    'Lights': 'lights',
    'Map': 'map',
    'TV': 'tv',
    'Ticket Gate': 'ticket_gate',
    'Poster': 'poster',
}

_CATEGORY_COLORS_RGB = {
    'Advertisement Board': (231, 76, 60),    # red
    'Exit Sign': (52, 152, 219),             # blue
    'Lights': (241, 196, 15),                # yellow
    'Map': (155, 89, 182),                   # purple
    'TV': (26, 188, 156),                    # teal
    'Ticket Gate': (230, 126, 34),           # orange
    'Poster': (236, 64, 122),                # pink
}


def _hsv_to_rgb(h, s, v):
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


def color_for_category(category, alpha=1.0):
    """Stable RGBA uint8 [r, g, b, a] for a category name (mirrors common_utils)."""
    rgb = _CATEGORY_COLORS_RGB.get(category)
    if rgb is None:
        key = str(category) if category is not None else 'unknown'
        h = (zlib.crc32(key.encode('utf-8')) * 0.6180339887498949) % 1.0
        r, g, b = _hsv_to_rgb(h, 0.80, 0.95)
    else:
        r, g, b = (c / 255.0 for c in rgb)
    return [int(round(r * 255)), int(round(g * 255)), int(round(b * 255)),
            int(round(alpha * 255))]


def category_slug(category):
    """Path-safe slug for a category name (mirrors common_utils)."""
    if category is None:
        return 'unknown'
    if category in _CATEGORY_SLUGS:
        return _CATEGORY_SLUGS[category]
    slug = re.sub(r'[^a-z0-9]+', '_', str(category).lower()).strip('_')
    return slug or 'unknown'


def rpy_deg_to_quaternion(roll_deg, pitch_deg, yaw_deg):
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


class InspectionMarker:
    """Load the colored global map into Rerun and expose mark_by_id /
    mark_by_category / clean_markings against the inspection DB."""

    def __init__(self, db_path=DEFAULT_DB, map_path=DEFAULT_MAP,
                 objects_dir=DEFAULT_OBJECTS_DIR, voxel=0.1,
                 spawn_viewer=True, connect_url=None, rrd_path=None,
                 load_map=True, leveling_rpy_deg=None):
        self.db_path = Path(db_path)
        self.map_path = Path(map_path) if map_path else None
        self.objects_dir = Path(objects_dir)
        self.voxel = voxel
        self.leveling_rpy_deg = (list(leveling_rpy_deg)
                                 if leveling_rpy_deg is not None
                                 else list(DEFAULT_LEVELLING_RPY_DEG))

        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row

        self._init_rerun(spawn_viewer, connect_url, rrd_path)
        if load_map and self.map_path:
            self._load_map()

    # ------------------------------------------------------------------
    # Rerun setup
    # ------------------------------------------------------------------

    def _init_rerun(self, spawn_viewer, connect_url, rrd_path):
        """Init the recording, attach sink(s), log the world frame + blueprint.

        Sink pattern mirrors rerun_bridge_node: spawn detached + GrpcSink, with
        an optional FileSink tee. A leveling rotation at world/leveled (viz-only)
        flattens the tilted-LiDAR map; map + marks are logged under it.
        """
        rr.init("inspection_marking")

        sinks = []
        if spawn_viewer:
            rr.spawn(connect=False)
            sinks.append(rr.GrpcSink())
        elif connect_url is not None:
            sinks.append(rr.GrpcSink(connect_url) if connect_url
                         else rr.GrpcSink())
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
            print("[rerun] WARNING: no sink - nothing will be visualized "
                  "(use --spawn / --connect / --rrd)")

        rr.log(WORLD, rr.ViewCoordinates.RIGHT_HAND_Z_UP, static=True)

        if any(abs(a) > 1e-6 for a in self.leveling_rpy_deg):
            q = rpy_deg_to_quaternion(*self.leveling_rpy_deg)
            rr.log(f"{WORLD}/leveled",
                   rr.Transform3D(rotation=rr.Quaternion(xyzw=q)),
                   static=True)
            base = f"{WORLD}/leveled"
            print(f"[rerun] leveling applied: RPY={self.leveling_rpy_deg} deg")
        else:
            base = WORLD
        self.base_path = base
        rr.log(f"{base}/camera_init",
               rr.Transform3D(translation=[0, 0, 0]), static=True)
        self.map_ent = f"{base}/camera_init/colored_map"
        self.marks_root = f"{base}/camera_init/marks"

        bp = rrb.Blueprint(
            rrb.Spatial3DView(origin=WORLD, name="Inspection map",
                             contents="$origin/**"),
            auto_layout=False, auto_views=False,
        )
        rr.send_blueprint(bp, make_active=True, make_default=True)

    def _load_map(self):
        """Load the photo-colored map PCD (xyz + rgb) and log it static."""
        if not self.map_path or not self.map_path.exists():
            print(f"[map] WARNING: {self.map_path} not found - continuing "
                  "without a map (marks only).")
            return
        print(f"[map] loading {self.map_path} ...")
        pcd = o3d.io.read_point_cloud(str(self.map_path))
        if len(pcd.points) == 0:
            print("[map] WARNING: empty cloud - continuing without a map.")
            return
        if self.voxel and self.voxel > 0.0:
            pcd = pcd.voxel_down_sample(float(self.voxel))
        pts = np.asarray(pcd.points, dtype=np.float32)
        rgb = np.asarray(pcd.colors, dtype=np.float32)  # (N,3) in [0,1]
        if rgb.shape[0] == pts.shape[0] and rgb.shape[0] > 0:
            colors = np.empty((pts.shape[0], 4), dtype=np.uint8)
            colors[:, :3] = np.clip(rgb * 255.0, 0, 255).astype(np.uint8)
            colors[:, 3] = 255
            rr.log(self.map_ent, rr.Points3D(pts, colors=colors), static=True)
        else:
            rr.log(self.map_ent, rr.Points3D(pts), static=True)
        print(f"[map] logged {pts.shape[0]} pts ({self.map_ent})")

    # ------------------------------------------------------------------
    # DB queries
    # ------------------------------------------------------------------

    def _query_object(self, object_id):
        cur = self.conn.execute(
            "SELECT o.id, c.name AS category FROM objects o "
            "LEFT JOIN categories c ON o.category_id = c.id WHERE o.id = ?",
            (object_id,))
        row = cur.fetchone()
        return dict(row) if row is not None else None

    def _query_objects_by_category(self, category):
        """Return (matched_name, rows). category may be exact name,
        case-insensitive name, or slug. rows ordered by id."""
        cats = self._categories()
        wanted = category.strip()
        wanted_slug = category_slug(wanted)
        match = None
        for _cid, name in cats:
            if name == wanted or name.lower() == wanted.lower() \
                    or category_slug(name) == wanted_slug:
                match = name
                break
        if match is None:
            return None, []
        cur = self.conn.execute(
            "SELECT o.id, c.name AS category FROM objects o "
            "JOIN categories c ON o.category_id = c.id WHERE c.name = ? "
            "ORDER BY o.id",
            (match,))
        return match, [dict(r) for r in cur.fetchall()]

    def _load_object_cloud(self, object_id):
        """Read objects/<id>.pcd -> (N,3) float32, or None if absent/empty."""
        path = self.objects_dir / f"{int(object_id)}.pcd"
        if not path.exists():
            return None
        pts = np.asarray(o3d.io.read_point_cloud(str(path)).points,
                         dtype=np.float32)
        return pts if pts.shape[0] > 0 else None

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    def mark_by_id(self, object_id):
        """Mark one object's segmented point cloud on the map."""
        row = self._query_object(object_id)
        if row is None:
            print(f"[mark_by_id] no object with id={object_id} "
                  f"(ids {self._min_object_id()}..{self._max_object_id()})")
            return
        pts = self._load_object_cloud(object_id)
        if pts is None:
            print(f"[mark_by_id] id={object_id}: no segmented cloud "
                  f"({self.objects_dir}/{object_id}.pcd)")
            return
        category = row['category'] or 'unknown'
        color = np.array(color_for_category(category), dtype=np.uint8)
        rr.log(f"{self.marks_root}/id_{int(object_id)}",
               rr.Points3D(pts, colors=np.tile(color, (pts.shape[0], 1))))
        print(f"[mark_by_id] id={object_id} {category}: {pts.shape[0]} pts")

    def mark_by_category(self, category):
        """Mark every object's segmented cloud of a category on the map."""
        match, rows = self._query_objects_by_category(category)
        if match is None:
            print(f"[mark_by_category] unknown category '{category}'. "
                  f"Available: {', '.join(n for _, n in self._categories())}")
            return
        color = np.array(color_for_category(match), dtype=np.uint8)
        chunks, ids = [], []
        for r in rows:
            pts = self._load_object_cloud(r['id'])
            if pts is not None:
                chunks.append(pts)
                ids.append(int(r['id']))
        if not chunks:
            print(f"[mark_by_category] '{match}': no segmented clouds found")
            return
        pts = np.concatenate(chunks)
        rr.log(f"{self.marks_root}/cat_{category_slug(match)}",
               rr.Points3D(pts, colors=np.tile(color, (pts.shape[0], 1))))
        print(f"[mark_by_category] '{match}': {len(ids)} objects, "
              f"{pts.shape[0]} pts (ids {ids[0]}..{ids[-1]})")

    def clean_markings(self):
        """Remove every mark from the viewer."""
        rr.log(self.marks_root, rr.Clear(recursive=True))
        print("[clean_markings] cleared all marks")

    def list_categories(self):
        """Print each category with its object count, plus the id range."""
        print("Categories (id -> name: objects):")
        total = 0
        for cid, name in self._categories():
            n = self.conn.execute(
                "SELECT COUNT(*) FROM objects WHERE category_id = ?", (cid,)
            ).fetchone()[0]
            print(f"  {cid:>2} -> {name:<22} {n}")
            total += n
        print(f"  total objects: {total}  (id range "
              f"{self._min_object_id()}..{self._max_object_id()})")

    # ------------------------------------------------------------------
    # Small DB helpers
    # ------------------------------------------------------------------

    def _categories(self):
        cur = self.conn.execute("SELECT id, name FROM categories ORDER BY id")
        return [(int(r['id']), r['name']) for r in cur.fetchall()]

    def _min_object_id(self):
        r = self.conn.execute("SELECT MIN(id) FROM objects").fetchone()
        return int(r[0]) if r[0] is not None else 0

    def _max_object_id(self):
        r = self.conn.execute("SELECT MAX(id) FROM objects").fetchone()
        return int(r[0]) if r[0] is not None else 0

    # ------------------------------------------------------------------
    # Interactive prompt
    # ------------------------------------------------------------------

    def repl(self):
        """Read commands line-by-line and dispatch to the API.

        Commands: id <n>, category <name>, clean, list, help, quit.
        category <name> may be the exact name, case-insensitive, or slug.
        """
        print("\nInspection marking REPL. Type 'help' for commands.")
        while True:
            try:
                line = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            parts = line.split(None, 1)
            cmd = parts[0].lower()
            arg = parts[1].strip() if len(parts) > 1 else ""
            if cmd in ("quit", "exit", "q"):
                break
            elif cmd == "id":
                try:
                    self.mark_by_id(int(arg))
                except ValueError:
                    print("  usage: id <integer object_id>")
            elif cmd in ("category", "cat", "c"):
                if not arg:
                    print("  usage: category <name>  (e.g. Lights, "
                          "'Advertisement Board', adboards)")
                else:
                    self.mark_by_category(arg)
            elif cmd in ("clean", "clear"):
                self.clean_markings()
            elif cmd in ("list", "ls"):
                self.list_categories()
            elif cmd in ("help", "?", "h"):
                self._print_help()
            else:
                print(f"  unknown command '{cmd}' - type 'help'")

    @staticmethod
    def _print_help():
        print("Commands:")
        print("  id <n>                 mark one object's segmented cloud")
        print("  category <name>        mark all objects of a category")
        print("                         (exact, case-insensitive, or slug)")
        print("  clean                  remove all marks")
        print("  list                   list categories + counts + id range")
        print("  quit                   exit")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self):
        try:
            rr.disconnect()
        except Exception:
            pass
        if self.conn:
            self.conn.close()
            self.conn = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Mark inspection objects' segmented clouds from "
                    "inspection_v2.db on the colored global map in Rerun.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Interactive prompt commands: id <n>, category <name>, "
               "clean, list, help, quit.",
    )
    p.add_argument("--db", default=DEFAULT_DB, help=f"inspection DB (default: {DEFAULT_DB})")
    p.add_argument("--map", default=DEFAULT_MAP,
                   help=f"colored-map PCD (default: {DEFAULT_MAP})")
    p.add_argument("--objects-dir", default=DEFAULT_OBJECTS_DIR,
                   help=f"per-object PCD dir (default: {DEFAULT_OBJECTS_DIR})")
    p.add_argument("--voxel", type=float, default=0.1,
                   help="voxel-downsample size in m for the map (0 = full; default 0.1)")
    p.add_argument("--no-map", action="store_true",
                   help="skip map loading (marks only)")
    p.add_argument("--spawn", dest="spawn_viewer", action="store_true",
                   default=True, help="spawn a Rerun viewer (default)")
    p.add_argument("--no-spawn", dest="spawn_viewer", action="store_false",
                   help="do not spawn; use --connect / --rrd instead")
    p.add_argument("--connect", nargs="?", const="", default=None, metavar="URL",
                   help="attach to an already-running viewer (no value = "
                        "localhost:9876). Implies --no-spawn unless --spawn given.")
    p.add_argument("--rrd", default=None, help="tee the run to a replayable .rrd")
    p.add_argument("--id", type=int, default=None,
                   help="one-shot: mark this object id (then prompt unless --once)")
    p.add_argument("--category", default=None,
                   help="one-shot: mark this category (then prompt unless --once)")
    p.add_argument("--once", action="store_true",
                   help="with --id/--category: mark and exit (no prompt)")
    p.add_argument("--leveling-rpy", type=float, nargs=3,
                   default=list(DEFAULT_LEVELLING_RPY_DEG),
                   metavar=("ROLL", "PITCH", "YAW"),
                   help="viz-only leveling rotation in deg [roll pitch yaw] "
                        f"(default: {' '.join(map(str, DEFAULT_LEVELLING_RPY_DEG))})")
    p.add_argument("--no-leveling", action="store_true",
                   help="disable the leveling rotation")
    args = p.parse_args(argv)

    if args.connect is not None and args.spawn_viewer:
        args.spawn_viewer = False
    leveling = [0.0, 0.0, 0.0] if args.no_leveling else args.leveling_rpy

    marker = InspectionMarker(
        db_path=args.db, map_path=args.map, objects_dir=args.objects_dir,
        voxel=args.voxel, spawn_viewer=args.spawn_viewer,
        connect_url=args.connect, rrd_path=args.rrd,
        load_map=not args.no_map, leveling_rpy_deg=leveling,
    )

    try:
        if args.id is not None:
            marker.mark_by_id(args.id)
        if args.category is not None:
            marker.mark_by_category(args.category)
        if args.once:
            if args.id is None and args.category is None:
                print("--once given but no --id/--category; nothing to do.")
            return 0
        marker.repl()
    finally:
        marker.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
