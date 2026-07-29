"""One-time offline extraction of the station map for the chatbot's Rerun viewer.

The grounding pipeline (`inspection_grounding`) builds:
- A photo-colored world map (`/world/leveled/camera_init/colored_map`) from
  camera-projected LiDAR points. It is RGB but only covers surfaces the camera
  actually saw and photographed within `colorize_max_depth`.
- A cumulative raw LiDAR map (`/world/leveled/camera_init/Laser_map`) from
  FAST-LIO. It spans the full station geometry the LiDAR swept, but is
  grayscale/intensity-only.

Both are logged as static `Points3D` in the grounding pipeline's multi-GB `.rrd`.
The chatbot cannot stream that whole recording per turn, and a file-loaded `.rrd`
lives in a *separate* Rerun recording from the chatbot's gRPC stream (two
recordings do not composite). So this script extracts just the final static map
snapshots, downsamples them to a manageable size, and saves them in a compact
.npz. The chatbot then logs both layers in its own recording:

- `world/map/colored` — the RGB photo-colored map
- `world/map/laser` — the gray LiDAR context map (fills in un-photographed areas)

The saved points are in the **raw camera_init frame** (the same frame the DB stores
object centroids/bboxes in). The chatbot pre-rotates them by the leveling matrix
(`RERUN_LEVELING_RPY_DEG`, default `0.0,20.0,0.0`) at log time, matching the
highlights and the grounding bridge's `world/leveled` convention.

Usage:
    .venv/bin/python scripts/extract_station_map.py \
        --rrd /path/to/output_mtr.rrd \
        --out data/station_map.npz \
        --max-points 1500000 \
        --max-laser-points 500000
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

# rerun's experimental reader is what's available in 0.35 for reading .rrd files.
from rerun.experimental import RrdReader


def _read_last_entity(rrd_path: Path, entity_substring: str) -> tuple[np.ndarray, np.ndarray] | None:
    """Return the *last* (positions, packed_colors) batch for an entity path.

    Static entities like `colored_map` and `Laser_map` are re-logged every
    frame/period. Each log is a snapshot of the cumulative map. The last chunk
    is the final state; earlier chunks are subsets or recolored duplicates.
    """
    reader = RrdReader(str(rrd_path))

    last_pts: np.ndarray | None = None
    last_cols: np.ndarray | None = None
    chunks = 0
    for chunk in reader.stream():
        entity_path = str(chunk.entity_path)
        if entity_substring not in entity_path:
            continue
        chunks += 1
        record_batch = chunk.to_record_batch()
        pts_rows: list[np.ndarray] = []
        col_rows: list[np.ndarray] = []
        for i, name in enumerate(record_batch.schema.names):
            if name == "Points3D:positions":
                col = record_batch.column(i)
                for row in col.to_pylist():
                    if row is None:
                        continue
                    pts_rows.append(np.asarray(row, dtype=np.float32).reshape(-1, 3))
            elif name == "Points3D:colors":
                col = record_batch.column(i)
                for row in col.to_pylist():
                    if row is None:
                        continue
                    col_rows.append(np.asarray(row, dtype=np.uint32))
        if pts_rows:
            last_pts = np.concatenate(pts_rows, axis=0)
        if col_rows:
            last_cols = np.concatenate(col_rows, axis=0)

    if last_pts is None:
        return None
    if last_cols is None:
        last_cols = np.full(last_pts.shape[0], 0xFF808080, dtype=np.uint32)
    elif last_cols.shape[0] != last_pts.shape[0]:
        raise RuntimeError(
            f"Mismatched counts for {entity_substring}: positions={last_pts.shape[0]}, "
            f"colors={last_cols.shape[0]}"
        )
    print(
        f"  {entity_substring}: read {chunks} chunks, "
        f"final snapshot {last_pts.shape[0]:,} points"
    )
    return last_pts, last_cols


def _decode_rgba(packed: np.ndarray) -> np.ndarray:
    """Convert packed 0xRRGGBBAA uint32 colors to uint8 Nx4 [R,G,B,A].

    Rerun 0.35's experimental RRD reader returns PackedRGBA32 values with the
    bytes in 0xRRGGBBAA order (R high byte, A low byte), not the 0xAABBGGRR
    order the previous decoder assumed. Decoding with the old order made every
    point red because the alpha byte is nearly always 255.
    """
    rgba = np.empty((packed.shape[0], 4), dtype=np.uint8)
    rgba[:, 0] = (packed >> 24) & 0xFF  # R
    rgba[:, 1] = (packed >> 16) & 0xFF  # G
    rgba[:, 2] = (packed >> 8) & 0xFF   # B
    rgba[:, 3] = (packed >> 0) & 0xFF   # A
    return rgba


def _downsample(pts: np.ndarray, cols: np.ndarray, max_points: int) -> tuple[np.ndarray, np.ndarray]:
    if pts.shape[0] <= max_points:
        return pts, cols
    stride = int(np.ceil(pts.shape[0] / max_points))
    return pts[::stride].copy(), cols[::stride].copy()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rrd", required=True, help="Path to the grounding pipeline .rrd recording.")
    parser.add_argument("--out", default="data/station_map.npz", help="Output .npz path (raw camera_init frame).")
    parser.add_argument("--max-points", type=int, default=1_500_000, help="Approx cap on colored map point count.")
    parser.add_argument("--max-laser-points", type=int, default=500_000, help="Approx cap on laser map point count.")
    args = parser.parse_args()

    rrd_path = Path(args.rrd).expanduser().resolve()
    out_path = Path(args.out).expanduser().resolve()

    print(f"Reading from: {rrd_path}")

    colored = _read_last_entity(rrd_path, "colored_map")
    if colored is None:
        raise RuntimeError(f"No colored_map entity found in {rrd_path}")
    pts_colored, cols_colored = colored
    print(f"  colored_map bounds: min={pts_colored.min(0)}  max={pts_colored.max(0)}  "
          f"range={pts_colored.max(0) - pts_colored.min(0)}")

    laser = _read_last_entity(rrd_path, "Laser_map")
    if laser is None:
        print("  Warning: no Laser_map entity found; output will contain colored map only")
        pts_laser = np.zeros((0, 3), dtype=np.float32)
        cols_laser = np.zeros((0, 4), dtype=np.uint8)
    else:
        pts_laser, cols_laser = laser
        print(f"  Laser_map bounds: min={pts_laser.min(0)}  max={pts_laser.max(0)}  "
              f"range={pts_laser.max(0) - pts_laser.min(0)}")

    pts_colored, cols_colored = _downsample(pts_colored, cols_colored, args.max_points)
    pts_laser, cols_laser = _downsample(pts_laser, cols_laser, args.max_laser_points)

    rgba_colored = _decode_rgba(cols_colored)
    rgba_laser = _decode_rgba(cols_laser)
    # The bridge intentionally dimmed the Laser_map (intensity * 0.3) so it
    # does not compete with the colored photo map. For the chatbot's static
    # context layer we brighten it a few times so the full station geometry is
    # visible without washing out the colored map.
    if rgba_laser.shape[0] > 0:
        bright = rgba_laser[:, :3].astype(np.float32) * 6.0
        np.clip(bright, 0, 255, out=bright)
        rgba_laser[:, :3] = bright.astype(np.uint8)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        out_path,
        positions=pts_colored.astype(np.float32, copy=False),
        colors=rgba_colored,
        laser_positions=pts_laser.astype(np.float32, copy=False),
        laser_colors=rgba_laser,
    )
    print(f"Saved -> {out_path}")
    print(f"  colored map: {pts_colored.shape[0]:,} points")
    print(f"  laser  map:  {pts_laser.shape[0]:,} points")
    print(f"  file size: {out_path.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
