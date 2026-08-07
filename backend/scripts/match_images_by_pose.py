#!/usr/bin/env python3
"""Pose-based image matching across two inspection runs.

For each image in a *source* set (by default the files in
``MTR Inspection Database/sampled_images/``, which are a subset of inspection 1,
named ``<image_id>.jpg``), find the image in a *target* inspection (default 2)
that was taken from the same viewpoint, by comparing the camera pose stored on
the ``images`` table (``tf_translation_{x,y,z}`` + quaternion
``tf_rotation_{x,y,z,w}``).

Both inspections live in the shared ``camera_init`` (FastLIO) global frame and
traverse the same route, so poses are directly comparable - no cross-run
alignment is needed.

Fisheye left/right lens assignment
---------------------------------
Consecutive captures form a left/right pair from the *same* pose - the two
images share an identical pose (translation AND quaternion) and the same
``timestamp_ns``. A naive "position % 2" parity does **not** work: a dropped
frame anywhere in an inspection flips L/R for every image after it, and since
all inspections share one folder/table the alternation does not reset cleanly.
(The real data has exactly such a drop: id 23 in inspection 1, id 584 in
inspection 2, each leaving 539/553 images with the wrong parity.)

We therefore assign L/R **by clustering images within each inspection on their
identical pose + timestamp**: every size-2 cluster is one L/R pair, with the
lower id = LEFT (the first capture of a pair is left). Size-1 clusters are
unpaired frames whose lens is ambiguous; they are excluded from matching.
Matching is then done per lens (LEFT source -> LEFT target, RIGHT -> RIGHT) so
lenses are never crossed - critical because an L and R of the same pose are
indistinguishable to a pose-only matcher and would otherwise pair off at
trans=0 to the wrong lens.

Matching is optimal 1:1 per parity class via the Hungarian algorithm
(``scipy.optimize.linear_sum_assignment``) on a cost of

    cost = translation_m + rot_weight * rotation_deg

with a threshold gate (``max_dist_m`` / ``max_rot_deg``) that drops pairs whose
nearest available partner is actually a different viewpoint.

Run with the backend venv so scipy/numpy are available, e.g.::

    backend/.venv/bin/python backend/scripts/match_images_by_pose.py \
        --target-inspection 2 --json pairs.json

To sample the source images on the fly at a fixed distance interval (via
``sample_images_along_trajectory.py``) before matching instead of relying on an
existing ``--sampled-dir``, add ``--sample-interval-m`` (and the inspection to
sample from with ``--sample-inspection``)::

backend/.venv/bin/python backend/scripts/match_images_by_pose.py \
        --target-inspection 2 --json pairs.json \
        --sample-interval-m 1.0 --sample-inspection 1

Full pipeline (sample on the fly + match + write pairs into
``abnormal_detections``) in one command::

    backend/.venv/bin/python backend/scripts/match_images_by_pose.py \
        --target-inspection 2 --json pairs.json --commit \
        --sample-interval-m 1.0 --sample-inspection 1

Full pipeline against ``inspection_v2.db`` at 0.5 m sampling with 2 m / 20 deg
gates, writing each matched pair as a merged image (src left, tgt right) into
``matched_images/``, showing a cv2 window, and printing a results summary (as
run to populate ``abnormal_detections``)::

    backend/scripts/match_images_by_pose.py \
        --db "MTR Inspection Database/inspection_v2.db" \
        --target-inspection 2 --json pairs_05_2m_20d.json --commit \
        --sample-interval-m 0.5 --sample-inspection 1 \
        --max-dist-m 2.0 --max-rot-deg 20 \
        --matched-dir "MTR Inspection Database/matched_images"

Add ``--no-show`` to skip the cv2 display window.

To instead plot the source vs target inspection trajectories with matplotlib
(save to a file with ``--plot-out``, or open interactively without it)::

    backend/scripts/match_images_by_pose.py \
        --db "MTR Inspection Database/inspection_v2.db" \
        --target-inspection 2 \
        --sample-interval-m 0.5 --sample-inspection 1 \
        --plot --plot-out trajectory_plot.png

Sampled source images are written to ``MTR Inspection Database/sampled_images/``
(default ``--sampled-dir``).
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

import numpy as np
from scipy.optimize import linear_sum_assignment

import sample_images_along_trajectory as sampler

# Repo root is two levels up from this script (backend/scripts/ -> repo root),
# so defaults resolve correctly regardless of the current working directory.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DB = _REPO_ROOT / "MTR Inspection Database" / "inspection_v2.db"
_DEFAULT_SAMPLED_DIR = _REPO_ROOT / "MTR Inspection Database" / "sampled_images"
_DEFAULT_IMAGE_DIR = _REPO_ROOT / "MTR Inspection Database" / "outputs" / "images"


def _load_inspection(
    conn: sqlite3.Connection, inspection_id: int
) -> tuple[list[dict[str, Any]], list[int]]:
    """All pose-bearing images of one inspection, ordered by id (capture order),
    with L/R lens assigned by pose-clustering.

    Returns ``(rows, unpaired_ids)``. Each row gets a ``parity`` of "L" or "R";
    images whose pose has no twin (dropped/lonely frame, lens ambiguous) are
    omitted from ``rows`` and returned in ``unpaired_ids``.
    """
    raw = conn.execute(
        """
        SELECT id, inspection_id, filename, timestamp_ns,
               tf_translation_x AS tx, tf_translation_y AS ty, tf_translation_z AS tz,
               tf_rotation_x AS rx, tf_rotation_y AS ry,
               tf_rotation_z AS rz, tf_rotation_w AS rw
        FROM images
        WHERE inspection_id = ? AND tf_translation_x IS NOT NULL
                                  AND tf_rotation_w IS NOT NULL
        ORDER BY id
        """,
        (inspection_id,),
    ).fetchall()
    rows = [dict(r) for r in raw]

    # Cluster on identical pose + timestamp. L/R pairs share the exact same
    # pose, so this groups them; distinct route viewpoints (centimetres apart)
    # never collide at this epsilon.
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for r in rows:
        key = (
            round(r["tx"], 4), round(r["ty"], 4), round(r["tz"], 4),
            round(r["rx"], 6), round(r["ry"], 6), round(r["rz"], 6), round(r["rw"], 6),
            r["timestamp_ns"],
        )
        groups.setdefault(key, []).append(r)

    out: list[dict[str, Any]] = []
    unpaired: list[int] = []
    for key, grp in groups.items():
        grp.sort(key=lambda d: d["id"])
        if len(grp) == 2:
            grp[0]["parity"] = "L"
            grp[1]["parity"] = "R"
            out.extend(grp)
        elif len(grp) == 1:
            unpaired.append(grp[0]["id"])
        else:
            # Unexpected (3+ images at one pose). Assign L to the first, R to
            # the rest, and warn so the data can be investigated.
            print(f"[warn] inspection {inspection_id}: pose group {key} has "
                  f"{len(grp)} images (ids {[g['id'] for g in grp]}); "
                  f"assigning L to first, R to rest.", file=sys.stderr)
            grp[0]["parity"] = "L"
            for g in grp[1:]:
                g["parity"] = "R"
            out.extend(grp)
    out.sort(key=lambda d: d["id"])
    return out, unpaired


def _quat_rotation_deg(
    q_src: np.ndarray, q_tgt: np.ndarray
) -> np.ndarray:
    """Geodesic rotation angle (degrees) between quaternions, broadcastable.

    ``q_src`` is (N, 4), ``q_tgt`` is (M, 4); returns (N, M). ``abs(dot)``
    folds the quaternion double-cover (q and -q are the same rotation).
    """
    dot = np.abs(q_src @ q_tgt.T)
    dot = np.clip(dot, 0.0, 1.0)
    return np.degrees(2.0 * np.arccos(dot))


def _match_class(
    sources: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    max_dist_m: float,
    max_rot_deg: float,
    rot_weight: float,
    allow_turnaround: bool = False,
) -> list[dict[str, Any]]:
    """Optimal 1:1 assignment of sources -> candidates within one parity class.

    Returns one dict per kept pair (those passing the threshold gate). Sources
    with no acceptable candidate are omitted here; the caller reports them.

    With ``allow_turnaround`` the candidates may be any parity (the L/R split
    is lifted) and each pair's rotation is taken as the smaller of the direct
    rotation vs. the candidate yaw-flipped by 180 deg about the (vertical) z
    axis. This models the robot driving back the same route: a source LEFT view
    then corresponds to a target RIGHT view (and vice versa).
    """
    if not sources or not candidates:
        return []

    P = np.array([[s["tx"], s["ty"], s["tz"]] for s in sources], dtype=float)
    C = np.array([[c["tx"], c["ty"], c["tz"]] for c in candidates], dtype=float)
    qP = np.array([[s["rx"], s["ry"], s["rz"], s["rw"]] for s in sources], dtype=float)
    qC = np.array([[c["rx"], c["ry"], c["rz"], c["rw"]] for c in candidates], dtype=float)

    trans = np.sqrt(((P[:, None, :] - C[None, :, :]) ** 2).sum(-1))  # (N, M)

    if allow_turnaround:
        # 180 deg yaw flip about z: (w,x,y,z) -> (-z, y, -x, w).
        qC_flip = np.empty_like(qC)
        qC_flip[:, 0] = -qC[:, 2]
        qC_flip[:, 1] = qC[:, 1]
        qC_flip[:, 2] = -qC[:, 0]
        qC_flip[:, 3] = qC[:, 3]
        rot = np.minimum(
            _quat_rotation_deg(qP, qC),
            _quat_rotation_deg(qP, qC_flip),
        )
    else:
        rot = _quat_rotation_deg(qP, qC)                              # (N, M)
    cost = trans + rot_weight * rot

    # Gate out infeasible pairs so the Hungarian solver avoids them when a
    # better assignment exists; results are re-checked against the gate below.
    infeasible = (trans > max_dist_m) | (rot > max_rot_deg)
    cost = np.where(infeasible, cost.max() + 1.0e6, cost)

    row_ind, col_ind = linear_sum_assignment(cost)
    pairs: list[dict[str, Any]] = []
    for i, j in zip(row_ind, col_ind):
        if trans[i, j] > max_dist_m or rot[i, j] > max_rot_deg:
            continue  # gated: nearest available partner is a different viewpoint
        s, c = sources[i], candidates[j]
        pairs.append(
            {
                "sampled_id": int(s["id"]),
                "target_id": int(c["id"]),
                "parity": s["parity"],
                "translation_m": float(trans[i, j]),
                "rotation_deg": float(rot[i, j]),
                "cost": float(cost[i, j]),
            }
        )
    return pairs


def _nearest_distance(
    src: dict[str, Any], candidates: list[dict[str, Any]], allow_turnaround: bool = False
) -> tuple[float, float, int | None]:
    """Nearest candidate's (translation_m, rotation_deg, id) for diagnostics.

    With ``allow_turnaround`` the rotation is the smaller of the direct vs.
    the yaw-flipped (180 deg about z) candidate, matching ``_match_class``.
    """
    if not candidates:
        return float("inf"), float("inf"), None
    P = np.array([[src["tx"], src["ty"], src["tz"]]], dtype=float)
    C = np.array([[c["tx"], c["ty"], c["tz"]] for c in candidates], dtype=float)
    qP = np.array([[src["rx"], src["ry"], src["rz"], src["rw"]]], dtype=float)
    qC = np.array([[c["rx"], c["ry"], c["rz"], c["rw"]] for c in candidates], dtype=float)
    trans = np.sqrt(((P[:, None, :] - C[None, :, :]) ** 2).sum(-1))[0]
    if allow_turnaround:
        qC_flip = np.empty_like(qC)
        qC_flip[:, 0] = -qC[:, 2]
        qC_flip[:, 1] = qC[:, 1]
        qC_flip[:, 2] = -qC[:, 0]
        qC_flip[:, 3] = qC[:, 3]
        rot = np.minimum(_quat_rotation_deg(qP, qC)[0],
                         _quat_rotation_deg(qP, qC_flip)[0])
    else:
        rot = _quat_rotation_deg(qP, qC)[0]
    j = int(np.argmin(trans))
    return float(trans[j]), float(rot[j]), int(candidates[j]["id"])


def _load_sampled_ids(sampled_dir: Path) -> list[int]:
    """Image ids from ``<id>.jpg`` filenames in ``sampled_dir``."""
    ids: list[int] = []
    for p in sorted(sampled_dir.iterdir()):
        if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png"):
            stem = p.stem
            if stem.isdigit():
                ids.append(int(stem))
    return sorted(ids)


def _resolve_source_inspection(conn: sqlite3.Connection, sampled_ids: list[int]) -> int | None:
    """Inspection id that the sampled images belong to (all should match)."""
    if not sampled_ids:
        return None
    placeholders = ",".join("?" * len(sampled_ids))
    row = conn.execute(
        f"SELECT inspection_id, COUNT(*) AS n FROM images "
        f"WHERE id IN ({placeholders}) GROUP BY inspection_id ORDER BY n DESC LIMIT 1",
        sampled_ids,
    ).fetchone()
    return int(row["inspection_id"]) if row else None


def _commit_pairs(
    conn: sqlite3.Connection, pairs: list[dict[str, Any]], skip_existing: bool
) -> int:
    """Insert matched pairs into abnormal_detections(gt_image, inspection_image).

    ``gt_image`` is the inspection-1 (reference) id, ``inspection_image`` the
    inspection-2 id. With ``skip_existing`` (default), pairs already present are
    left untouched so LLM-annotated rows are never clobbered. Returns the number
    inserted.
    """
    # Assign ids explicitly (starting at MAX(id)+1) rather than relying on the
    # table's AUTOINCREMENT counter. AUTOINCREMENT keeps a monotonic high-water
    # mark in sqlite_sequence that never decreases, so after rows are deleted the
    # raw id would otherwise resume from a stale value (e.g. 526) instead of
    # continuing sequentially from the surviving rows.
    next_id = conn.execute(
        "SELECT COALESCE(MAX(id), 0) + 1 FROM abnormal_detections"
    ).fetchone()[0]
    inserted = 0
    for p in pairs:
        gt, src = p["sampled_id"], p["target_id"]
        if skip_existing:
            exists = conn.execute(
                "SELECT 1 FROM abnormal_detections "
                "WHERE gt_image = ? AND inspection_image = ? LIMIT 1",
                (gt, src),
            ).fetchone()
            if exists:
                continue
        conn.execute(
            "INSERT INTO abnormal_detections "
            "(id, gt_image, inspection_image, status, summary, viewpoint_change) "
            "VALUES (?, ?, ?, 'NOT_PROCESSED', '', 0)",
            (next_id, gt, src),
        )
        next_id += 1
        inserted += 1
    conn.commit()
    return inserted


def _copy_matched_images(
    pairs: list[dict[str, Any]],
    sampled_dir: Path,
    image_dir: Path,
    out_dir: Path,
    show: bool = True,
) -> list[Path]:
    """Write each matched pair as a single merged image (source left, target
    right) into ``out_dir``, named ``<src_id>__<tgt_id>.jpg``.

    Optionally spawn a cv2 window showing each merged pair. Returns the list of
    merged files written. Requires opencv-python (``cv2``).
    """
    if not pairs:
        return []
    try:
        import cv2
    except ImportError as e:  # pragma: no cover
        print(f"[error] --matched-dir needs opencv (cv2): missing: {e}", file=sys.stderr)
        raise

    out_dir.mkdir(parents=True, exist_ok=True)
    merged: list[Path] = []
    for pp in pairs:
        src_id, tgt_id = pp["sampled_id"], pp["target_id"]
        src = sampled_dir / f"{src_id}.jpg"
        tgt = image_dir / f"{tgt_id}.jpg"
        if not src.exists() or not tgt.exists():
            print(f"[warn] matched pair {src_id}->{tgt_id}: missing image "
                  f"({src} / {tgt}), skipped", file=sys.stderr)
            continue
        srcimg = cv2.imread(str(src))
        tgtimg = cv2.imread(str(tgt))
        if srcimg is None or tgtimg is None:
            print(f"[warn] matched pair {src_id}->{tgt_id}: could not decode "
                  f"image, skipped", file=sys.stderr)
            continue
        # Place both on a common canvas, side by side (src left, tgt right).
        height = max(srcimg.shape[0], tgtimg.shape[0])
        width = srcimg.shape[1] + tgtimg.shape[1]
        canvas = np.full((height, width, 3), 128, dtype=np.uint8)
        canvas[:srcimg.shape[0], :srcimg.shape[1]] = srcimg
        canvas[:tgtimg.shape[0], srcimg.shape[1]:] = tgtimg
        merged_path = out_dir / f"{src_id}__{tgt_id}.jpg"
        if not cv2.imwrite(str(merged_path), canvas):
            print(f"[warn] could not write merged image: {merged_path}", file=sys.stderr)
            continue
        merged.append(merged_path)

    if show and merged:
        print("[info] opening cv2 window of matched pairs (press any key to advance, "
              "ESC to quit)")
        window = "matched pairs (src | tgt)"
        for mp in merged:
            img = cv2.imread(str(mp))
            if img is None:
                continue
            cv2.imshow(window, img)
            key = cv2.waitKey(0) & 0xFF
            if key == 27:  # ESC
                break
        cv2.destroyAllWindows()
    return merged


def _resample_images(
    conn: sqlite3.Connection,
    out_dir: Path,
    src_dir: Path,
    inspection: int,
    interval_m: float,
    start_index: int,
    lens: str,
    keep_existing: bool,
) -> int:
    """Populate ``out_dir`` with images sampled along ``inspection``'s trajectory.

    Reuses ``sample_images_along_trajectory``'s pose clustering and greedy
    arc-length sampling, then copies the picked <id>.jpg files from ``src_dir``
    into ``out_dir`` (cleared first unless ``keep_existing``). Returns the number
    of images copied, or -1 on error.
    """
    views, unpaired = sampler._load_pairs(conn, inspection)
    if not views:
        print(f"[error] no pose-bearing viewpoints in inspection {inspection}", file=sys.stderr)
        return -1
    if unpaired:
        print(f"[warn] re-sampling inspection {inspection}: {len(unpaired)} unpaired "
              f"(lens-ambiguous) image(s) excluded: {unpaired}", file=sys.stderr)

    picked = sampler._sample_by_arclength(views, interval_m, start_index)

    sampled_ids: list[int] = []
    for v in picked:
        if lens == "left":
            sampled_ids.append(v["left_id"])
        elif lens == "right":
            if v["right_id"] is None:
                print(f"[warn] view left_id={v['left_id']} has no right pair; skipping",
                      file=sys.stderr)
                continue
            sampled_ids.append(v["right_id"])
        else:
            sampled_ids.append(v["left_id"])
            if v["right_id"] is not None:
                sampled_ids.append(v["right_id"])
    sampled_ids = sorted(set(sampled_ids))

    out_dir.mkdir(parents=True, exist_ok=True)
    if not keep_existing:
        for f in out_dir.iterdir():
            if f.is_file():
                f.unlink()

    copied = 0
    missing: list[int] = []
    for sid in sampled_ids:
        src = src_dir / f"{sid}.jpg"
        dst = out_dir / f"{sid}.jpg"
        if not src.exists():
            missing.append(sid)
            continue
        shutil.copy2(src, dst)
        copied += 1
    if missing:
        print(f"[warn] {len(missing)} sampled id(s) missing in {src_dir}: {missing}",
              file=sys.stderr)
    return copied


def _plot_trajectories(
    conn: sqlite3.Connection,
    source_inspection: int,
    target_inspection: int,
    out_path: str | None = None,
) -> None:
    """Plot source vs target inspection trajectories (tf x/y) with matplotlib.

    Each inspection's images are projected to the ground plane using their
    ``tf_translation_x`` / ``tf_translation_y`` and drawn as points joined by
    line segments. Source and target are coloured differently. If ``out_path``
    is given the figure is saved there instead of shown interactively.
    """
    import matplotlib.pyplot as plt

    def _pos(insp: int) -> tuple[list[float], list[float]]:
        xs, ys = [], []
        for row in conn.execute(
            "SELECT tf_translation_x AS tx, tf_translation_y AS ty "
            "FROM images WHERE inspection_id = ? "
            "AND tf_translation_x IS NOT NULL ORDER BY id",
            (insp,),
        ):
            xs.append(row["tx"])
            ys.append(row["ty"])
        return xs, ys

    plt.figure(figsize=(10, 6))
    for insp, color, label in (
        (source_inspection, "tab:blue", f"Inspection {source_inspection} (source)"),
        (target_inspection, "tab:orange", f"Inspection {target_inspection} (target)"),
    ):
        xs, ys = _pos(insp)
        if xs:
            plt.plot(xs, ys, "-o", color=color, markersize=2, linewidth=1, label=label)
    plt.xlabel("tf x (m)")
    plt.ylabel("tf y (m)")
    plt.title("Inspection trajectories (camera_init frame)")
    plt.legend()
    plt.grid(alpha=0.3)
    plt.axis("equal")
    plt.tight_layout()
    if out_path:
        plt.savefig(out_path, dpi=150)
        print(f"[info] saved trajectory plot to {out_path}")
    else:
        plt.show()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=str(_DEFAULT_DB), help="Path to inspection_v2_mtr_new.db")
    p.add_argument(
        "--sampled-dir",
        default=str(_DEFAULT_SAMPLED_DIR),
        help="Directory of <id>.jpg source images from inspection 1",
    )
    p.add_argument(
        "--source-inspection",
        type=int,
        default=None,
        help="Override: use ALL images of this inspection as the source instead of --sampled-dir",
    )
    p.add_argument(
        "--sample-interval-m",
        type=float,
        default=None,
        help="Re-populate --sampled-dir by sampling the source inspection trajectory "
             "at this fixed arc-length interval (metres) via sample_images_along_trajectory.py",
    )
    p.add_argument(
        "--sample-inspection",
        type=int,
        default=1,
        help="Inspection id to sample from when --sample-interval-m is set (default 1)",
    )
    p.add_argument(
        "--sample-start-index",
        type=int,
        default=0,
        help="Index (in capture order) of the first sampled viewpoint (default 0)",
    )
    p.add_argument(
        "--sample-lens",
        choices=["left", "right", "both"],
        default="left",
        help="Which lens of each L/R pair to copy when sampling (default left)",
    )
    p.add_argument(
        "--keep-sampled",
        action="store_true",
        help="When --sample-interval-m is set, do not clear --sampled-dir before writing",
    )
    p.add_argument("--target-inspection", type=int, default=2, help="Inspection id to match against (default 2)")
    p.add_argument("--max-dist-m", type=float, default=1.5, help="Max translation (m) for a valid pair")
    p.add_argument("--max-rot-deg", type=float, default=12.0, help="Max rotation (deg) for a valid pair")
    p.add_argument("--rot-weight", type=float, default=0.1, help="m-per-deg rotation weight in the cost")
    p.add_argument(
        "--left-only",
        action="store_true",
        help="Only match LEFT-parity images (drop RIGHT sources)",
    )
    p.add_argument(
        "--allow-turnaround",
        action="store_true",
        help="Allow L<->R cross-lens pairing, comparing each pair's rotation against "
             "a 180 deg yaw-flipped candidate (handles the robot driving back the "
             "same route)",
    )
    p.add_argument("--json", dest="json_path", default=None, help="Write the pair list to this JSON file")
    p.add_argument("--image-dir", default=str(_DEFAULT_IMAGE_DIR), help="Image folder for reported target file paths")
    p.add_argument(
        "--matched-dir",
        default=None,
        help="Write each matched pair as a merged image (src left, tgt right) "
             "into this folder as <src_id>__<tgt_id>.jpg",
    )
    p.add_argument(
        "--no-show",
        action="store_true",
        help="With --matched-dir, do not open the cv2 display window",
    )
    p.add_argument(
        "--plot",
        action="store_true",
        help="Plot the source and target inspection trajectories with matplotlib",
    )
    p.add_argument(
        "--plot-out",
        default=None,
        help="Save the trajectory plot to this file (default: show interactively)",
    )
    p.add_argument(
        "--commit",
        action="store_true",
        help="Insert matched pairs into abnormal_detections (off by default; does not clobber existing rows)",
    )
    p.add_argument(
        "--no-skip-existing",
        dest="skip_existing",
        action="store_false",
        help="With --commit, also re-insert pairs that already exist in abnormal_detections",
    )
    p.set_defaults(skip_existing=True)
    args = p.parse_args(argv)

    db_path = Path(args.db).resolve()
    if not db_path.exists():
        print(f"[error] database not found: {db_path}", file=sys.stderr)
        return 2
    sampled_dir = Path(args.sampled_dir).resolve()

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    if args.plot:
        src_insp = args.sample_inspection if args.sample_interval_m is not None \
            else (args.source_inspection if args.source_inspection is not None
                  else args.sample_inspection)
        _plot_trajectories(conn, src_insp, args.target_inspection,
                           out_path=args.plot_out)
    if args.sample_interval_m is not None:
        n = _resample_images(
            conn,
            sampled_dir,
            Path(args.image_dir),
            args.sample_inspection,
            args.sample_interval_m,
            args.sample_start_index,
            args.sample_lens,
            args.keep_sampled,
        )
        if n < 0:
            conn.close()
            return 2
        print(f"[info] sampled {n} image(s) from inspection {args.sample_inspection} "
              f"at {args.sample_interval_m} m into {sampled_dir}")
    try:
        target_rows, tgt_unpaired = _load_inspection(conn, args.target_inspection)
        if not target_rows:
            print(f"[error] no pose-bearing L/R pairs in inspection {args.target_inspection}", file=sys.stderr)
            return 2
        if tgt_unpaired:
            print(f"[info] target inspection {args.target_inspection}: {len(tgt_unpaired)} unpaired "
                  f"image(s) excluded (ambiguous lens): {tgt_unpaired}", file=sys.stderr)
        by_parity_tgt = {"L": [r for r in target_rows if r["parity"] == "L"],
                         "R": [r for r in target_rows if r["parity"] == "R"]}

        # Resolve the source set.
        if args.source_inspection is not None:
            source_rows, src_unpaired = _load_inspection(conn, args.source_inspection)
            if src_unpaired:
                print(f"[info] source inspection {args.source_inspection}: {len(src_unpaired)} unpaired "
                      f"image(s) excluded (ambiguous lens): {src_unpaired}", file=sys.stderr)
            src_label = f"all of inspection {args.source_inspection}"
        else:
            if not sampled_dir.exists():
                print(f"[error] sampled dir not found: {sampled_dir}", file=sys.stderr)
                return 2
            source_ids = _load_sampled_ids(sampled_dir)
            if not source_ids:
                print(f"[error] no <id>.jpg files found in {sampled_dir}", file=sys.stderr)
                return 2
            src_insp = _resolve_source_inspection(conn, source_ids)
            if src_insp is None:
                print("[error] could not resolve which inspection the sampled images belong to", file=sys.stderr)
                return 2
            # L/R is assigned by pose-clustering within the source inspection.
            all_src, src_unpaired = _load_inspection(conn, src_insp)
            id_to_row = {r["id"]: r for r in all_src}
            unpaired_set = set(src_unpaired)
            source_rows: list[dict[str, Any]] = []
            missing: list[int] = []
            ambiguous: list[int] = []
            for sid in source_ids:
                r = id_to_row.get(sid)
                if r is not None:
                    source_rows.append(r)
                elif sid in unpaired_set:
                    ambiguous.append(sid)
                else:
                    missing.append(sid)
            if missing:
                print(f"[warn] {len(missing)} sampled id(s) not found in inspection "
                      f"{src_insp} (skipped): {missing}", file=sys.stderr)
            if ambiguous:
                print(f"[warn] {len(ambiguous)} sampled id(s) are unpaired in inspection "
                      f"{src_insp} (lens ambiguous, skipped): {ambiguous}", file=sys.stderr)
            src_label = f"{len(source_rows)} sampled image(s) from inspection {src_insp}"

        if args.left_only:
            dropped = [r for r in source_rows if r["parity"] != "L"]
            if dropped:
                print(f"[info] --left-only: dropping {len(dropped)} RIGHT-parity source image(s): "
                      f"{[r['id'] for r in dropped]}", file=sys.stderr)
            source_rows = [r for r in source_rows if r["parity"] == "L"]

        by_parity_src = {"L": [r for r in source_rows if r["parity"] == "L"],
                         "R": [r for r in source_rows if r["parity"] == "R"]}

        print(f"[info] db: {db_path}")
        print(f"[info] source: {src_label}")
        print(f"[info] target: inspection {args.target_inspection} "
              f"({len(target_rows)} imgs: {len(by_parity_tgt['L'])} L / {len(by_parity_tgt['R'])} R)")
        print(f"[info] source parity: {len(by_parity_src['L'])} L / {len(by_parity_src['R'])} R")
        print(f"[info] gate: max_dist={args.max_dist_m} m, max_rot={args.max_rot_deg} deg, "
              f"rot_weight={args.rot_weight}")

        all_pairs: list[dict[str, Any]] = []
        matched_src_ids: set[int] = set()
        if args.allow_turnaround:
            # Lift the L/R split entirely: every source can match any target,
            # with rotation measured against the direct and the yaw-flipped view.
            cands = target_rows
            pairs = _match_class(source_rows, cands, args.max_dist_m,
                                 args.max_rot_deg, args.rot_weight,
                                 allow_turnaround=True)
            for pp in pairs:
                matched_src_ids.add(pp["sampled_id"])
            all_pairs = pairs
            print(f"[info] turnaround matching: matched {len(pairs)}/{len(source_rows)} "
                  f"source image(s) against {len(cands)} candidate(s)")
        else:
            for parity in ("L", "R"):
                srcs = by_parity_src[parity]
                cands = by_parity_tgt[parity]
                pairs = _match_class(srcs, cands, args.max_dist_m, args.max_rot_deg,
                                     args.rot_weight)
                for pp in pairs:
                    matched_src_ids.add(pp["sampled_id"])
                all_pairs.extend(pairs)
                print(f"[info] parity {parity}: matched {len(pairs)}/{len(srcs)} "
                      f"source image(s) against {len(cands)} candidate(s)")

        # Candidate pool for unmatched diagnostics (both parity when turnaround).
        diag_tgt = target_rows if args.allow_turnaround else by_parity_tgt

        # Diagnostics for unmatched sources: nearest candidate distance.
        unmatched = [r for r in source_rows if r["id"] not in matched_src_ids]
        if unmatched:
            print(f"\n[info] {len(unmatched)} unmatched source image(s) - "
                  f"nearest candidate:")
            for r in sorted(unmatched, key=lambda x: x["id"]):
                cand_pool = diag_tgt if args.allow_turnaround else diag_tgt[r["parity"]]
                nt, nr, nid = _nearest_distance(r, cand_pool, args.allow_turnaround)
                print(f"  src id={r['id']:>4} ({r['parity']})  nearest target id={nid}  "
                      f"trans={nt:.3f} m  rot={nr:.2f} deg  (over gate -> no match)")

        all_pairs.sort(key=lambda pp: pp["sampled_id"])

        print("\n=== matched pairs ===")
        print(f"{'src_id':>7} {'parity':>6} {'tgt_id':>7} {'trans_m':>9} {'rot_deg':>8} {'cost':>7}")
        for pp in all_pairs:
            print(f"{pp['sampled_id']:>7} {pp['parity']:>6} {pp['target_id']:>7} "
                  f"{pp['translation_m']:>9.3f} {pp['rotation_deg']:>8.2f} {pp['cost']:>7.3f}")

        if args.json_path:
            image_dir = Path(args.image_dir)
            for pp in all_pairs:
                pp["sampled_file"] = str(sampled_dir / f"{pp['sampled_id']}.jpg") if sampled_dir.exists() else None
                pp["target_file"] = str(image_dir / f"{pp['target_id']}.jpg")
            Path(args.json_path).write_text(json.dumps(all_pairs, indent=2))
            print(f"[info] wrote {len(all_pairs)} pair(s) to {args.json_path}")

        if args.matched_dir:
            matched_dir = Path(args.matched_dir).resolve()
            matched_dir.mkdir(parents=True, exist_ok=True)
            merged = _copy_matched_images(
                all_pairs, sampled_dir, Path(args.image_dir), matched_dir,
                show=not args.no_show,
            )
            print(f"[info] wrote {len(merged)} merged pair image(s) to {matched_dir}")
            if merged and args.no_show:
                print("[info] --no-show set: skipping cv2 display window")

        # Results summary: matching stats + names of unmatched images.
        print("\n=== results summary ===")
        print(f"  source images:      {len(source_rows)}")
        print(f"  matched pairs:      {len(all_pairs)}")
        print(f"  unmatched images:   {len(unmatched)}")
        if all_pairs:
            t = [pp["translation_m"] for pp in all_pairs]
            r = [pp["rotation_deg"] for pp in all_pairs]
            print(f"  translation (m):   min={min(t):.3f} med={sorted(t)[len(t)//2]:.3f} "
                  f"max={max(t):.3f}")
            print(f"  rotation (deg):    min={min(r):.2f} med={sorted(r)[len(r)//2]:.2f} "
                  f"max={max(r):.2f}")
        if unmatched:
            print("\n  unmatched image(s) (name -> id | nearest cost):")
            for r in sorted(unmatched, key=lambda x: x["id"]):
                cand_pool = diag_tgt if args.allow_turnaround else diag_tgt[r["parity"]]
                nt, nr, nid = _nearest_distance(r, cand_pool, args.allow_turnaround)
                cost = nt + args.rot_weight * nr
                name = r["filename"] or f"{r['id']}.jpg"
                print(f"    {name}  (id={r['id']}, parity={r['parity']}, "
                      f"nearest tgt={nid}, trans={nt:.3f} m, rot={nr:.2f} deg, "
                      f"cost={cost:.3f})")

        if args.commit:
            n = _commit_pairs(conn, all_pairs, args.skip_existing)
            mode = "inserted" if args.skip_existing else "inserted (incl. duplicates)"
            print(f"[info] {n} pair(s) {mode} into abnormal_detections")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
