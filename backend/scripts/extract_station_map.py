"""One-time offline extraction of the station map for the chatbot's Rerun viewer.

The grounding pipeline (`inspection_grounding`) builds a photo-colored station map
and records it (plus trajectory, bboxes, per-frame clouds, images) to a large
`.rrd` (several GB). The chatbot can't stream that whole recording to its viewer
every turn, and a file-loaded `.rrd` lives in a *separate* Rerun recording from the
chatbot's gRPC stream (the two are not auto-composited in the 3D view).

So this script pulls just the cumulative colored map
(`/world/leveled/camera_init/colored_map`, a static `Points3D`) out of the `.rrd`,
downsamples it to a manageable point count, and saves both the raw camera_init-frame
coordinates and the per-point RGBA colors to a compact `.npz`. The chatbot then loads
this `.npz` and logs it as a static `world/map` entity in its *own* recording — same
recording as the highlights, so the ticket gate lands directly on the colored station
map.

The saved points are in the **raw camera_init frame** (the same frame the DB stores
object centroids/bboxes in). The chatbot pre-rotates them by the leveling matrix
(`RERUN_LEVELING_RPY_DEG`, default `0.0,20.0,0.0`) at log time, matching the
highlights and the grounding bridge's `world/leveled` convention.

Usage:
    .venv/bin/python scripts/extract_station_map.py \
        --rrd /path/to/output_mtr.rrd \
        --out data/station_map.npz \
        --max-points 1500000
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

# rerun's experimental reader is what's available in 0.35 for reading .rrd files.
from rerun.experimental import RrdReader


def _collect(rrd_path: Path) -> tuple[np.ndarray, np.ndarray]:
    reader = RrdReader(str(rrd_path))

    chunks = 0
    all_pts: list[np.ndarray] = []
    all_cols: list[np.ndarray] = []
    # Legacy RRD files without a footer do not support reader.store(); stream()
    # directly and filter for colored_map chunks.
    for chunk in reader.stream():
        entity_path = str(chunk.entity_path)
        if "colored_map" not in entity_path:
            continue
        record_batch = chunk.to_record_batch()
        chunks += 1
        for i, name in enumerate(record_batch.schema.names):
            if name == "Points3D:positions":
                for row in record_batch.column(i).to_pylist():
                    if row is None:
                        continue
                    # `row` is a list of [x, y, z] triples (fixed-size-list<3>).
                    all_pts.append(np.asarray(row, dtype=np.float32).reshape(-1, 3))
            elif name == "Points3D:colors":
                for row in record_batch.column(i).to_pylist():
                    if row is None:
                        continue
                    # `row` is a list of packed RGBA uint32 values in 0xRRGGBBAA order.
                    all_cols.append(np.asarray(row, dtype=np.uint32))

    if not all_pts:
        raise RuntimeError(f"No colored_map positions found in {rrd_path}")
    pts = np.concatenate(all_pts, axis=0)
    if all_cols:
        cols = np.concatenate(all_cols, axis=0)
        if cols.shape[0] != pts.shape[0]:
            raise RuntimeError(
                f"Mismatched counts: positions={pts.shape[0]}, colors={cols.shape[0]}"
            )
    else:
        # Fallback if colors are missing (should not happen for a colored map).
        cols = np.full(pts.shape[0], 0xFF808080, dtype=np.uint32)
    print(f"  read {chunks} chunks, {pts.shape[0]:,} raw points, {cols.shape[0]:,} colors")
    return pts, cols


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
    parser.add_argument("--max-points", type=int, default=1_500_000, help="Approx cap on output point count.")
    args = parser.parse_args()

    rrd_path = Path(args.rrd).expanduser().resolve()
    out_path = Path(args.out).expanduser().resolve()
    print(f"Reading colored_map from: {rrd_path}")
    pts, cols = _collect(rrd_path)
    print(f"  raw bounds: min={pts.min(0)}  max={pts.max(0)}  range={pts.max(0) - pts.min(0)}")

    pts_down, cols_down = _downsample(pts, cols, args.max_points)
    rgba = _decode_rgba(cols_down)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out_path, positions=pts_down.astype(np.float32, copy=False), colors=rgba)
    print(f"Saved {pts_down.shape[0]:,} points (positions + RGBA colors) -> {out_path}")
    print(f"  file size: {out_path.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()