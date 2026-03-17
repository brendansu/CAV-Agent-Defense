"""Episode builder for VeReMi Extension Sybil-attack traces.

Converts raw receiver-centric traceJSON logs into structured
episode-level JSONL suitable for LLM-based collusion / Sybil detection.

Data flow
---------
  data/raw/<AttackType>_<hours>/VeReMi_<t1>_<t2>_<date>/
      traceGroundTruthJSON-<run>.json      (type 4 – global sent BSMs)
      traceJSON-<vid>-<nid>-A<at>-<t>-<run>.json  (type 2 ego + type 3 rcvd)
                           │
                    episode_builder.py
                           │
                           ▼
  data/processed/episodes/<AttackType>_VeReMi_<t1>_<t2>_<date>.jsonl

Each output line is one episode JSON object.

Usage
-----
  python -m src.data.episode_builder \
      --raw_dir data/raw/GridSybil_0709 \
      --out_dir data/processed/episodes \
      --window_sec 10 --step_sec 5

Notes on feature philosophy
---------------------------
* Detection-side features use only information available to a receiver:
  senderPseudo, pos, spd, acl, hed, timing.
* The `sender` field (physical vehicle ID) is available in VeReMi trace
  files but would NOT exist in a real deployment.  It is used ONLY to
  produce ground-truth labels, never as a detection feature.
* Upstream / downstream flow and global traffic topology are not
  computable from a single receiver log; we approximate congestion with
  local speed statistics and pseudo-density.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ── tiny math helpers ───────────────────────────────────────────────

def _speed_scalar(spd: List[float]) -> float:
    return math.hypot(spd[0], spd[1])


def _mean(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _median(xs: List[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else 0.5 * (s[m - 1] + s[m])


def _std(xs: List[float]) -> float:
    if len(xs) < 2:
        return 0.0
    mu = _mean(xs)
    return math.sqrt(_mean([(v - mu) ** 2 for v in xs]))


def _round_floats(obj: Any, p: int = 2) -> Any:
    """Recursively round every float in nested dicts / lists.

    Also replaces inf / -inf with None so the output is valid JSON.
    """
    if isinstance(obj, float):
        if math.isinf(obj) or math.isnan(obj):
            return None
        return round(obj, p)
    if isinstance(obj, list):
        return [_round_floats(x, p) for x in obj]
    if isinstance(obj, dict):
        return {k: _round_floats(v, p) for k, v in obj.items()}
    return obj


# ── ground truth loader ─────────────────────────────────────────────

def load_ground_truth(gt_path: Path) -> Dict:
    """Parse ``traceGroundTruthJSON-*.json`` (type 4 records).

    Returns
    -------
    dict with keys
        pseudo_to_sender : dict[int, int]
            senderPseudo → physical sender ID.
        sender_pseudos   : dict[int, set[int]]
            sender → all pseudonyms it ever used.
        sybil_senders    : set[int]
            Physical IDs that used more than one pseudonym.
    """
    pseudo_to_sender: Dict[int, int] = {}
    sender_pseudos: Dict[int, set] = defaultdict(set)

    with open(gt_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("type") != 4:
                continue
            sid = rec["sender"]
            pid = rec["senderPseudo"]
            pseudo_to_sender[pid] = sid
            sender_pseudos[sid].add(pid)

    sybil_senders = {s for s, ps in sender_pseudos.items() if len(ps) > 1}
    return {
        "pseudo_to_sender": pseudo_to_sender,
        "sender_pseudos": {k: v for k, v in sender_pseudos.items()},
        "sybil_senders": sybil_senders,
    }


# ── file-name parsing ───────────────────────────────────────────────

def parse_trace_filename(name: str) -> Dict[str, Any]:
    """Extract metadata from ``traceJSON-<vid>-<nid>-A<at>-<sec>-<run>.json``."""
    stem = name.replace(".json", "")
    parts = stem.split("-")
    return {
        "vehicle_id": int(parts[1]),
        "neighbor_id": int(parts[2]),
        "attack_flag": int(parts[3].lstrip("A")),
        "start_sec": int(parts[4]),
        "run_idx": int(parts[5]),
    }


# ── receiver log I/O ────────────────────────────────────────────────

def load_receiver_log(path: Path) -> List[Dict]:
    records: List[Dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records


# ── sub-builders ─────────────────────────────────────────────────────

def _ego_summary(ego_states: List[Dict]) -> Dict:
    if not ego_states:
        return {"num_updates": 0}
    positions = [e["pos"] for e in ego_states]
    speeds = [_speed_scalar(e["spd"]) for e in ego_states]
    disp = math.hypot(
        positions[-1][0] - positions[0][0],
        positions[-1][1] - positions[0][1],
    )
    return {
        "num_updates": len(ego_states),
        "start_pos": positions[0][:2],
        "end_pos": positions[-1][:2],
        "displacement": disp,
        "avg_speed": _mean(speeds),
        "speed_std": _std(speeds),
    }


def _pseudo_track(msgs: List[Dict]) -> Dict:
    """Behavioural summary for one senderPseudo over the window."""
    msgs = sorted(msgs, key=lambda m: m["rcvTime"])
    times = [m["rcvTime"] for m in msgs]
    poses = [m["pos"] for m in msgs]
    spds = [_speed_scalar(m["spd"]) for m in msgs]
    acls = [_speed_scalar(m["acl"]) for m in msgs]
    heds = [math.atan2(m["hed"][1], m["hed"][0]) for m in msgs]

    xs = [p[0] for p in poses]
    ys = [p[1] for p in poses]
    pos_spread = math.hypot(max(xs) - min(xs), max(ys) - min(ys))

    dur = times[-1] - times[0] if len(times) > 1 else 0.0
    msg_rate = len(msgs) / dur if dur > 0 else float("inf")

    iat = [times[i] - times[i - 1] for i in range(1, len(times))]

    return {
        "num_msgs": len(msgs),
        "time_span": [times[0], times[-1]],
        "duration": dur,
        "msg_rate": msg_rate,
        "iat_mean": _mean(iat),
        "iat_std": _std(iat),
        "avg_pos": [_mean(xs), _mean(ys)],
        "pos_spread": pos_spread,
        "avg_speed": _mean(spds),
        "speed_std": _std(spds),
        "min_speed": min(spds) if spds else 0.0,
        "max_speed": max(spds) if spds else 0.0,
        "avg_acl": _mean(acls),
        "acl_std": _std(acls),
        "heading_std": _std(heds),
    }


# ── coordination heuristics / pseudo-group clustering ────────────────

def _pair_similarity(ta: Dict, tb: Dict) -> Dict:
    """Pairwise behavioural similarity between two pseudo tracks."""
    pos_dist = math.hypot(
        ta["avg_pos"][0] - tb["avg_pos"][0],
        ta["avg_pos"][1] - tb["avg_pos"][1],
    )
    speed_diff = abs(ta["avg_speed"] - tb["avg_speed"])
    acl_diff = abs(ta["avg_acl"] - tb["avg_acl"])

    a0, a1 = ta["time_span"]
    b0, b1 = tb["time_span"]
    overlap = max(0.0, min(a1, b1) - max(a0, b0))
    union = max(a1, b1) - min(a0, b0)
    temporal_iou = overlap / union if union > 0 else 0.0

    return {
        "pos_dist": pos_dist,
        "speed_diff": speed_diff,
        "acl_diff": acl_diff,
        "temporal_iou": temporal_iou,
    }


def _cluster_pseudos(
    tracks: Dict[int, Dict],
    pos_thr: float = 80.0,
    spd_thr: float = 5.0,
) -> List[Dict]:
    """Greedy single-linkage clustering by spatial + kinematic proximity.

    Two pseudonyms are merged into the same group when their avg_pos
    distance < *pos_thr* **and** avg_speed difference < *spd_thr*.
    """
    keys = list(tracks.keys())
    parent: Dict[int, int] = {k: k for k in keys}

    def _find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(a: int, b: int) -> None:
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            sim = _pair_similarity(tracks[keys[i]], tracks[keys[j]])
            if sim["pos_dist"] < pos_thr and sim["speed_diff"] < spd_thr:
                _union(keys[i], keys[j])

    groups_map: Dict[int, List[int]] = defaultdict(list)
    for k in keys:
        groups_map[_find(k)].append(k)

    groups: List[Dict] = []
    for members in groups_map.values():
        ts = [tracks[m] for m in members]
        avg_spds = [t["avg_speed"] for t in ts]
        speed_cv = _std(avg_spds) / (_mean(avg_spds) + 1e-9)
        groups.append({
            "pseudos": [str(m) for m in members],
            "size": len(members),
            "avg_pos": [
                _mean([t["avg_pos"][0] for t in ts]),
                _mean([t["avg_pos"][1] for t in ts]),
            ],
            "avg_speed": _mean(avg_spds),
            "speed_cv": speed_cv,
            "total_msgs": sum(t["num_msgs"] for t in ts),
        })

    groups.sort(key=lambda g: g["size"], reverse=True)
    for idx, g in enumerate(groups):
        g["group_id"] = f"G{idx + 1}"
    return groups


# ── core episode builder ────────────────────────────────────────────

def build_episode(
    records: List[Dict],
    *,
    run_id: str,
    receiver_id: int,
    attack_flag: int,
    window_start: float,
    window_end: float,
    gt_info: Optional[Dict] = None,
    precision: int = 2,
    pos_cluster_thr: float = 80.0,
    spd_cluster_thr: float = 5.0,
) -> Optional[Dict]:
    """Build one episode from *records* within ``[window_start, window_end]``.

    Parameters
    ----------
    records : list[dict]
        Full receiver log (type 2 + type 3 records).
    run_id : str
        Human-readable run identifier.
    receiver_id : int
        Physical vehicle ID of the receiver.
    attack_flag : int
        Attack-type code from the trace filename (0 = benign receiver).
    window_start, window_end : float
        Simulation-time boundaries for the episode.
    gt_info : dict, optional
        Output of :func:`load_ground_truth`.
    precision : int
        Decimal places kept after rounding.
    pos_cluster_thr, spd_cluster_thr : float
        Thresholds for pseudo-group clustering.

    Returns
    -------
    dict or None
        Structured episode; ``None`` when the window contains no BSMs.
    """
    window_dur = window_end - window_start

    # ---- 1. time-window filter ----
    win = [
        r for r in records
        if window_start <= r.get("rcvTime", r.get("sendTime", 0.0)) <= window_end
    ]
    ego_states = [r for r in win if r.get("type") == 2]
    bsms = [r for r in win if r.get("type") == 3]
    if not bsms:
        return None

    # ---- 2. ego summary ----
    ego = _ego_summary(ego_states)

    # ---- 3. region-level statistics ----
    all_spds = [_speed_scalar(m["spd"]) for m in bsms]
    unique_pseudos = {m["senderPseudo"] for m in bsms}
    unique_senders = {m["sender"] for m in bsms}

    region = {
        "num_bsms": len(bsms),
        "num_unique_pseudos": len(unique_pseudos),
        "num_unique_senders": len(unique_senders),
        "pseudo_sender_ratio": (
            len(unique_pseudos) / len(unique_senders)
            if unique_senders else 0.0
        ),
        "avg_speed": _mean(all_spds),
        "median_speed": _median(all_spds),
        "speed_std": _std(all_spds),
        "min_speed": min(all_spds),
        "max_speed": max(all_spds),
    }

    # ---- 4. per-pseudonym tracks ----
    by_pseudo: Dict[int, List[Dict]] = defaultdict(list)
    for m in bsms:
        by_pseudo[m["senderPseudo"]].append(m)

    pseudo_tracks = {p: _pseudo_track(ms) for p, ms in by_pseudo.items()}

    # ---- 5. coordination-heuristic clustering ----
    pseudo_groups = _cluster_pseudos(
        pseudo_tracks,
        pos_thr=pos_cluster_thr,
        spd_thr=spd_cluster_thr,
    )

    # ---- 6. macro congestion indicators ----
    low_spd_frac = sum(1 for s in all_spds if s < 2.0) / len(all_spds)
    multi_groups = [g for g in pseudo_groups if g["size"] > 1]

    macro = {
        "low_speed_fraction": low_spd_frac,
        "congestion_level": (
            "high" if low_spd_frac > 0.6
            else "medium" if low_spd_frac > 0.3
            else "low"
        ),
        "num_multi_id_groups": len(multi_groups),
        "largest_group_size": max(
            (g["size"] for g in pseudo_groups), default=0
        ),
    }

    # ---- 7. ground-truth labels (training only) ----
    #
    # Two complementary labelling strategies:
    #   a) GT-file path: look up each pseudo in the ground-truth mapping.
    #   b) Local-sender path: build sender->pseudos from the `sender`
    #      field embedded in type-3 records (available in VeReMi but NOT
    #      in real V2X).  This catches pseudos the GT file may omit.

    sender_to_pseudos_local: Dict[int, set] = defaultdict(set)
    for m in bsms:
        sender_to_pseudos_local[m["sender"]].add(m["senderPseudo"])
    local_sybil_senders = {
        s for s, ps in sender_to_pseudos_local.items() if len(ps) > 1
    }
    local_sybil_pseudos: set = set()
    for s in local_sybil_senders:
        local_sybil_pseudos |= sender_to_pseudos_local[s]

    label: Optional[Dict] = None
    if gt_info is not None:
        p2s = gt_info["pseudo_to_sender"]
        sybil_gt = gt_info["sybil_senders"]

        gt_attacker_phys: set = set()
        gt_sybil_pseudos: set = set()
        for pid in unique_pseudos:
            phys = p2s.get(pid)
            if phys is not None and phys in sybil_gt:
                gt_attacker_phys.add(phys)
                gt_sybil_pseudos.add(pid)

        all_sybil_senders = gt_attacker_phys | local_sybil_senders
        all_sybil_pseudos = gt_sybil_pseudos | local_sybil_pseudos

        label = {
            "has_sybil": len(all_sybil_senders) > 0,
            "num_attacker_vehicles": len(all_sybil_senders),
            "sybil_pseudo_count": len(all_sybil_pseudos),
            "sybil_pseudo_fraction": (
                len(all_sybil_pseudos) / len(unique_pseudos)
                if unique_pseudos else 0.0
            ),
        }
    else:
        if local_sybil_senders:
            label = {
                "has_sybil": True,
                "num_attacker_vehicles": len(local_sybil_senders),
                "sybil_pseudo_count": len(local_sybil_pseudos),
                "sybil_pseudo_fraction": (
                    len(local_sybil_pseudos) / len(unique_pseudos)
                    if unique_pseudos else 0.0
                ),
            }

    # ---- assemble ----
    episode: Dict[str, Any] = {
        "meta": {
            "run_id": run_id,
            "receiver_id": receiver_id,
            "receiver_is_attacker": attack_flag != 0,
            "attack_flag": attack_flag,
            "window": {
                "start": window_start,
                "end": window_end,
                "duration": window_dur,
            },
        },
        "ego": ego,
        "region": region,
        "pseudo_tracks": {str(k): v for k, v in pseudo_tracks.items()},
        "pseudo_groups": pseudo_groups,
        "macro": macro,
    }
    if label is not None:
        episode["label"] = label

    return _round_floats(episode, p=precision)


# ── sliding-window driver per receiver ───────────────────────────────

def episodes_from_receiver(
    trace_path: Path,
    run_id: str,
    gt_info: Optional[Dict] = None,
    window_sec: float = 10.0,
    step_sec: float = 5.0,
    precision: int = 2,
    pos_cluster_thr: float = 80.0,
    spd_cluster_thr: float = 5.0,
) -> List[Dict]:
    """Slide a window over one receiver file and yield episodes."""
    meta = parse_trace_filename(trace_path.name)
    records = load_receiver_log(trace_path)
    if not records:
        return []

    times = [r.get("rcvTime", r.get("sendTime", 0.0)) for r in records]
    t_lo, t_hi = min(times), max(times)

    eps: List[Dict] = []
    ws = t_lo
    while ws + window_sec <= t_hi + step_sec:
        we = ws + window_sec
        ep = build_episode(
            records,
            run_id=run_id,
            receiver_id=meta["vehicle_id"],
            attack_flag=meta["attack_flag"],
            window_start=ws,
            window_end=we,
            gt_info=gt_info,
            precision=precision,
            pos_cluster_thr=pos_cluster_thr,
            spd_cluster_thr=spd_cluster_thr,
        )
        if ep is not None:
            eps.append(ep)
        ws += step_sec
    return eps


# ── run-level batch processor ────────────────────────────────────────

def process_run(
    run_dir: Path,
    out_path: Path,
    *,
    window_sec: float = 10.0,
    step_sec: float = 5.0,
    precision: int = 2,
    max_receivers: Optional[int] = None,
    pos_cluster_thr: float = 80.0,
    spd_cluster_thr: float = 5.0,
) -> int:
    """Process every receiver trace in *run_dir*, append episodes to *out_path*.

    Returns the number of episodes written.
    """
    gt_files = list(run_dir.glob("traceGroundTruthJSON-*.json"))
    gt_info = None
    if gt_files:
        print(f"  Loading ground truth: {gt_files[0].name} ...", flush=True)
        gt_info = load_ground_truth(gt_files[0])
        n_pseudo = len(gt_info["pseudo_to_sender"])
        n_sybil = len(gt_info["sybil_senders"])
        print(
            f"  GT ready: {n_pseudo} pseudo->sender, {n_sybil} Sybil senders",
            flush=True,
        )

    traces = sorted(run_dir.glob("traceJSON-*.json"))
    if max_receivers is not None:
        traces = traces[:max_receivers]

    run_id = f"{run_dir.parent.name}/{run_dir.name}"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with open(out_path, "a", encoding="utf-8") as fout:
        for i, tf in enumerate(traces):
            eps = episodes_from_receiver(
                tf,
                run_id,
                gt_info=gt_info,
                window_sec=window_sec,
                step_sec=step_sec,
                precision=precision,
                pos_cluster_thr=pos_cluster_thr,
                spd_cluster_thr=spd_cluster_thr,
            )
            for ep in eps:
                fout.write(json.dumps(ep, ensure_ascii=False) + "\n")
            total += len(eps)
            if (i + 1) % 20 == 0 or i + 1 == len(traces):
                print(
                    f"    [{i + 1}/{len(traces)}] receivers done, "
                    f"{total} episodes so far",
                    flush=True,
                )
    return total


# ── CLI ──────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Build episode-level JSONL from VeReMi traceJSON files.",
    )
    ap.add_argument(
        "--raw_dir",
        type=str,
        required=True,
        help="Attack-type folder, e.g. data/raw/GridSybil_0709",
    )
    ap.add_argument(
        "--out_dir",
        type=str,
        default="data/processed/episodes",
    )
    ap.add_argument("--window_sec", type=float, default=10.0)
    ap.add_argument("--step_sec", type=float, default=5.0)
    ap.add_argument("--precision", type=int, default=2)
    ap.add_argument(
        "--max_receivers",
        type=int,
        default=None,
        help="Cap receivers per run (for quick debugging)",
    )
    ap.add_argument("--pos_cluster_thr", type=float, default=80.0)
    ap.add_argument("--spd_cluster_thr", type=float, default=5.0)
    args = ap.parse_args()

    raw_dir = Path(args.raw_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    run_dirs = sorted(
        d for d in raw_dir.iterdir()
        if d.is_dir() and d.name.startswith("VeReMi_")
    )
    if not run_dirs:
        print(f"No VeReMi_* directories in {raw_dir}")
        return

    attack_label = raw_dir.name
    print(f"Attack type: {attack_label}  |  {len(run_dirs)} run(s)", flush=True)

    grand = 0
    for rd in run_dirs:
        out_file = out_dir / f"{attack_label}_{rd.name}.jsonl"
        print(f"\n>> Run: {rd.name}", flush=True)
        print(f"   Output: {out_file}", flush=True)
        n = process_run(
            rd,
            out_file,
            window_sec=args.window_sec,
            step_sec=args.step_sec,
            precision=args.precision,
            max_receivers=args.max_receivers,
            pos_cluster_thr=args.pos_cluster_thr,
            spd_cluster_thr=args.spd_cluster_thr,
        )
        print(f"   Done: {n} episodes", flush=True)
        grand += n

    print(f"\nAll done. Total episodes: {grand}", flush=True)


if __name__ == "__main__":
    main()
