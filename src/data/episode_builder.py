"""Episode builder for VeReMi Extension GridSybil traces.

Converts receiver-centric `traceJSON-*` logs into episode-level JSONL for
GridSybil / ghost-traffic experiments.

Data flow
---------
  data/raw/<AttackType>_<hours>/VeReMi_<t1>_<t2>_<date>/
      traceGroundTruthJSON-<run>.json
      traceJSON-<vid>-<nid>-A<at>-<t>-<run>.json
                           │
                    episode_builder.py
                           │
                           ▼
  data/processed/episodes/<AttackType>_VeReMi_<t1>_<t2>_<date>.jsonl

Each output line is one episode JSON object.

Feature philosophy
------------------
* Detection-side embeddings must use only receiver-visible information:
  `senderPseudo`, `pos`, `spd`, `acl`, `hed`, and timing.
* The `sender` field is used only for labels / analysis because it would not be
  available in a real deployment.
* Global road topology is unavailable, so congestion is approximated through
  local speed distributions and low-speed multi-ID clusters.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional


LOW_SPEED_THR = 2.0
MED_SPEED_THR = 8.0
SHORT_LIVED_FRACTION_THR = 0.3


# ── tiny math helpers ───────────────────────────────────────────────

def _speed_scalar(spd: List[float]) -> float:
    return math.hypot(spd[0], spd[1])


def _heading_angle(hed: List[float]) -> float:
    return math.atan2(hed[1], hed[0])


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


def _angle_diff(a: float, b: float) -> float:
    diff = a - b
    while diff <= -math.pi:
        diff += 2.0 * math.pi
    while diff > math.pi:
        diff -= 2.0 * math.pi
    return diff


def _circular_mean(xs: List[float]) -> float:
    if not xs:
        return 0.0
    s = sum(math.sin(x) for x in xs)
    c = sum(math.cos(x) for x in xs)
    return math.atan2(s, c)


def _circular_std(xs: List[float]) -> float:
    if len(xs) < 2:
        return 0.0
    mu = _circular_mean(xs)
    diffs = [_angle_diff(x, mu) for x in xs]
    return math.sqrt(_mean([d ** 2 for d in diffs]))


def _distance2d(a: List[float], b: List[float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _bbox_spread(points: List[List[float]]) -> float:
    if not points:
        return 0.0
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return math.hypot(max(xs) - min(xs), max(ys) - min(ys))


def _bucket_fraction(values: List[float], lower: float, upper: Optional[float]) -> float:
    if not values:
        return 0.0
    if upper is None:
        count = sum(1 for v in values if v >= lower)
    else:
        count = sum(1 for v in values if lower <= v < upper)
    return count / len(values)


def _round_floats(obj: Any, p: int = 2) -> Any:
    """Recursively round every float in nested dicts / lists."""
    if isinstance(obj, float):
        if math.isinf(obj) or math.isnan(obj):
            return None
        return round(obj, p)
    if isinstance(obj, list):
        return [_round_floats(x, p) for x in obj]
    if isinstance(obj, dict):
        return {k: _round_floats(v, p) for k, v in obj.items()}
    return obj


def _traffic_regime_from_attack_type(attack_type: str) -> str:
    if attack_type.endswith("0709"):
        return "peak"
    if attack_type.endswith("1416"):
        return "off_peak"
    return "unknown"


# ── ground truth loader ─────────────────────────────────────────────

def load_ground_truth(gt_path: Path) -> Dict:
    """Parse ``traceGroundTruthJSON-*.json`` and derive Sybil senders."""
    pseudo_to_sender: Dict[int, int] = {}
    sender_pseudos: Dict[int, set[int]] = defaultdict(set)

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
    headings = [_heading_angle(e["hed"]) for e in ego_states]
    disp = _distance2d(positions[0], positions[-1])

    return {
        "num_updates": len(ego_states),
        "start_pos": positions[0][:2],
        "end_pos": positions[-1][:2],
        "displacement": disp,
        "avg_speed": _mean(speeds),
        "speed_std": _std(speeds),
        "avg_heading": _circular_mean(headings),
        "heading_std": _circular_std(headings),
    }


def _pseudo_track(msgs: List[Dict], window_dur: float) -> Dict:
    """Behavioural summary for one senderPseudo over the window."""
    msgs = sorted(msgs, key=lambda m: m["rcvTime"])
    times = [m["rcvTime"] for m in msgs]
    poses = [m["pos"] for m in msgs]
    spds = [_speed_scalar(m["spd"]) for m in msgs]
    acls = [_speed_scalar(m["acl"]) for m in msgs]
    heds = [_heading_angle(m["hed"]) for m in msgs]

    duration = times[-1] - times[0] if len(times) > 1 else 0.0
    lifetime_fraction = duration / window_dur if window_dur > 0 else 0.0
    msg_rate = len(msgs) / duration if duration > 0 else float("inf")
    iat = [times[i] - times[i - 1] for i in range(1, len(times))]

    avg_pos = [
        _mean([p[0] for p in poses]),
        _mean([p[1] for p in poses]),
    ]

    return {
        "num_msgs": len(msgs),
        "time_span": [times[0], times[-1]],
        "duration": duration,
        "lifetime_fraction": lifetime_fraction,
        "msg_rate": msg_rate,
        "iat_mean": _mean(iat),
        "iat_std": _std(iat),
        "avg_pos": avg_pos,
        "pos_spread": _bbox_spread([p[:2] for p in poses]),
        "avg_speed": _mean(spds),
        "speed_std": _std(spds),
        "min_speed": min(spds) if spds else 0.0,
        "max_speed": max(spds) if spds else 0.0,
        "avg_acl": _mean(acls),
        "acl_std": _std(acls),
        "avg_heading": _circular_mean(heds),
        "heading_std": _circular_std(heds),
    }


# ── coordination heuristics / pseudo-group clustering ────────────────

def _pair_similarity(ta: Dict, tb: Dict) -> Dict:
    """Pairwise similarity between two pseudo tracks."""
    a0, a1 = ta["time_span"]
    b0, b1 = tb["time_span"]
    overlap = max(0.0, min(a1, b1) - max(a0, b0))
    union = max(a1, b1) - min(a0, b0)

    return {
        "pos_dist": _distance2d(ta["avg_pos"], tb["avg_pos"]),
        "speed_diff": abs(ta["avg_speed"] - tb["avg_speed"]),
        "acl_diff": abs(ta["avg_acl"] - tb["avg_acl"]),
        "temporal_iou": overlap / union if union > 0 else 0.0,
    }


def _cluster_pseudos(
    tracks: Dict[int, Dict],
    ego_pos: List[float],
    window_dur: float,
    pos_thr: float = 80.0,
    spd_thr: float = 5.0,
    time_thr: float = 0.2,
) -> List[Dict]:
    """Cluster pseudonyms by spatial, speed, and temporal overlap."""
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
            if (
                sim["pos_dist"] < pos_thr
                and sim["speed_diff"] < spd_thr
                and sim["temporal_iou"] > time_thr
            ):
                _union(keys[i], keys[j])

    groups_map: Dict[int, List[int]] = defaultdict(list)
    for k in keys:
        groups_map[_find(k)].append(k)

    groups: List[Dict] = []
    for members in groups_map.values():
        ts = [tracks[m] for m in members]
        member_positions = [t["avg_pos"] for t in ts]
        avg_pos = [
            _mean([p[0] for p in member_positions]),
            _mean([p[1] for p in member_positions]),
        ]
        avg_spds = [t["avg_speed"] for t in ts]
        msg_rates = [t["msg_rate"] for t in ts if t["msg_rate"] is not None]
        headings = [t["avg_heading"] for t in ts]
        starts = [t["time_span"][0] for t in ts]
        ends = [t["time_span"][1] for t in ts]
        time_span = [min(starts), max(ends)]
        duration = time_span[1] - time_span[0]
        radius = max((_distance2d(p, avg_pos) for p in member_positions), default=0.0)

        groups.append({
            "pseudos": [str(m) for m in members],
            "size": len(members),
            "avg_pos": avg_pos,
            "avg_speed": _mean(avg_spds),
            "speed_cv": _std(avg_spds) / (_mean(avg_spds) + 1e-9),
            "total_msgs": sum(t["num_msgs"] for t in ts),
            "group_pos_spread": _bbox_spread(member_positions),
            "group_radius": radius,
            "group_density_proxy": len(members) / (radius + 1.0),
            "group_time_span": time_span,
            "group_duration": duration,
            "group_lifetime_fraction": duration / window_dur if window_dur > 0 else 0.0,
            "avg_heading": _circular_mean(headings),
            "group_heading_std": _circular_std(headings),
            "group_msg_rate_cv": _std(msg_rates) / (_mean(msg_rates) + 1e-9) if msg_rates else 0.0,
            "distance_to_ego": _distance2d(avg_pos, ego_pos),
        })

    groups.sort(key=lambda g: (g["size"], g["total_msgs"]), reverse=True)
    for idx, group in enumerate(groups, start=1):
        group["group_id"] = f"G{idx}"
    return groups


# ── core episode builder ────────────────────────────────────────────

def build_episode(
    records: List[Dict],
    *,
    run_id: str,
    receiver_id: int,
    attack_flag: int,
    attack_type: str,
    source_folder: str,
    traffic_regime: str,
    window_start: float,
    window_end: float,
    gt_info: Optional[Dict] = None,
    precision: int = 2,
    pos_cluster_thr: float = 80.0,
    spd_cluster_thr: float = 5.0,
    time_cluster_thr: float = 0.2,
) -> Optional[Dict]:
    """Build one episode from *records* within ``[window_start, window_end]``."""
    window_dur = window_end - window_start

    win = [
        r for r in records
        if window_start <= r.get("rcvTime", r.get("sendTime", 0.0)) <= window_end
    ]
    ego_states = [r for r in win if r.get("type") == 2]
    bsms = [r for r in win if r.get("type") == 3]
    if not bsms:
        return None

    ego = _ego_summary(ego_states)
    ego_pos = ego["end_pos"] if "end_pos" in ego else bsms[0]["pos"][:2]

    by_pseudo: Dict[int, List[Dict]] = defaultdict(list)
    for msg in bsms:
        by_pseudo[msg["senderPseudo"]].append(msg)
    pseudo_tracks = {p: _pseudo_track(msgs, window_dur) for p, msgs in by_pseudo.items()}

    all_spds = [_speed_scalar(m["spd"]) for m in bsms]
    unique_pseudos = set(by_pseudo.keys())
    single_message_pseudos = [t for t in pseudo_tracks.values() if t["num_msgs"] == 1]
    short_lived_pseudos = [
        t for t in pseudo_tracks.values()
        if t["lifetime_fraction"] < SHORT_LIVED_FRACTION_THR
    ]

    region = {
        "num_bsms": len(bsms),
        "num_unique_pseudos": len(unique_pseudos),
        "avg_speed": _mean(all_spds),
        "median_speed": _median(all_spds),
        "speed_std": _std(all_spds),
        "min_speed": min(all_spds),
        "max_speed": max(all_spds),
        "slow_msg_fraction": _bucket_fraction(all_spds, 0.0, LOW_SPEED_THR),
        "medium_msg_fraction": _bucket_fraction(all_spds, LOW_SPEED_THR, MED_SPEED_THR),
        "fast_msg_fraction": _bucket_fraction(all_spds, MED_SPEED_THR, None),
        "single_message_pseudo_fraction": (
            len(single_message_pseudos) / len(unique_pseudos) if unique_pseudos else 0.0
        ),
        "short_lived_pseudo_fraction": (
            len(short_lived_pseudos) / len(unique_pseudos) if unique_pseudos else 0.0
        ),
    }

    pseudo_groups = _cluster_pseudos(
        pseudo_tracks,
        ego_pos=ego_pos,
        window_dur=window_dur,
        pos_thr=pos_cluster_thr,
        spd_thr=spd_cluster_thr,
        time_thr=time_cluster_thr,
    )

    low_speed_groups = [g for g in pseudo_groups if g["avg_speed"] < LOW_SPEED_THR]
    multi_groups = [g for g in pseudo_groups if g["size"] > 1]
    largest_low_speed_group = max(
        low_speed_groups,
        key=lambda g: (g["size"], g["total_msgs"]),
        default=None,
    )

    macro = {
        "low_speed_fraction": region["slow_msg_fraction"],
        "congestion_level": (
            "high" if region["slow_msg_fraction"] > 0.6
            else "medium" if region["slow_msg_fraction"] > 0.3
            else "low"
        ),
        "num_groups": len(pseudo_groups),
        "num_multi_id_groups": len(multi_groups),
        "largest_group_size": max((g["size"] for g in pseudo_groups), default=0),
        "largest_low_speed_group_size": largest_low_speed_group["size"] if largest_low_speed_group else 0,
        "largest_low_speed_group_msgs": largest_low_speed_group["total_msgs"] if largest_low_speed_group else 0,
        "largest_low_speed_group_avg_speed": largest_low_speed_group["avg_speed"] if largest_low_speed_group else 0.0,
        "fraction_msgs_in_low_speed_groups": (
            sum(g["total_msgs"] for g in low_speed_groups) / len(bsms) if bsms else 0.0
        ),
        "fraction_pseudos_in_low_speed_groups": (
            sum(g["size"] for g in low_speed_groups) / len(unique_pseudos) if unique_pseudos else 0.0
        ),
    }

    sender_to_pseudos_local: Dict[int, set[int]] = defaultdict(set)
    for msg in bsms:
        sender_to_pseudos_local[msg["sender"]].add(msg["senderPseudo"])
    local_sybil_senders = {s for s, pseudos in sender_to_pseudos_local.items() if len(pseudos) > 1}
    local_sybil_pseudos: set[int] = set()
    for sender_id in local_sybil_senders:
        local_sybil_pseudos |= sender_to_pseudos_local[sender_id]

    attacker_vehicle_ids: List[int] = sorted(local_sybil_senders)
    sybil_pseudos: set[int] = set(local_sybil_pseudos)
    if gt_info is not None:
        p2s = gt_info["pseudo_to_sender"]
        sybil_gt = gt_info["sybil_senders"]
        for pseudo in unique_pseudos:
            phys = p2s.get(pseudo)
            if phys is not None and phys in sybil_gt:
                attacker_vehicle_ids.append(phys)
                sybil_pseudos.add(pseudo)

    attacker_vehicle_ids = sorted(set(attacker_vehicle_ids))
    label = {
        "contains_sybil_attacker": len(attacker_vehicle_ids) > 0,
        "num_attacker_vehicles": len(attacker_vehicle_ids),
        "attacker_vehicle_ids": attacker_vehicle_ids,
        "num_sybil_pseudos": len(sybil_pseudos),
        "sybil_pseudo_fraction": (
            len(sybil_pseudos) / len(unique_pseudos) if unique_pseudos else 0.0
        ),
    }

    episode: Dict[str, Any] = {
        "meta": {
            "run_id": run_id,
            "attack_type": attack_type,
            "source_folder": source_folder,
            "traffic_regime": traffic_regime,
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
        "label": label,
    }
    return _round_floats(episode, p=precision)


# ── sliding-window driver per receiver ───────────────────────────────

def episodes_from_receiver(
    trace_path: Path,
    run_id: str,
    attack_type: str,
    source_folder: str,
    traffic_regime: str,
    gt_info: Optional[Dict] = None,
    window_sec: float = 10.0,
    step_sec: float = 5.0,
    precision: int = 2,
    pos_cluster_thr: float = 80.0,
    spd_cluster_thr: float = 5.0,
    time_cluster_thr: float = 0.2,
) -> List[Dict]:
    """Slide a window over one receiver file and yield episodes."""
    meta = parse_trace_filename(trace_path.name)
    records = load_receiver_log(trace_path)
    if not records:
        return []

    times = [r.get("rcvTime", r.get("sendTime", 0.0)) for r in records]
    t_lo, t_hi = min(times), max(times)

    episodes: List[Dict] = []
    ws = t_lo
    while ws + window_sec <= t_hi + step_sec:
        we = ws + window_sec
        episode = build_episode(
            records,
            run_id=run_id,
            receiver_id=meta["vehicle_id"],
            attack_flag=meta["attack_flag"],
            attack_type=attack_type,
            source_folder=source_folder,
            traffic_regime=traffic_regime,
            window_start=ws,
            window_end=we,
            gt_info=gt_info,
            precision=precision,
            pos_cluster_thr=pos_cluster_thr,
            spd_cluster_thr=spd_cluster_thr,
            time_cluster_thr=time_cluster_thr,
        )
        if episode is not None:
            episodes.append(episode)
        ws += step_sec
    return episodes


# ── run-level batch processor ────────────────────────────────────────

def process_run(
    run_dir: Path,
    out_path: Path,
    *,
    attack_type: str,
    traffic_regime: str,
    window_sec: float = 10.0,
    step_sec: float = 5.0,
    precision: int = 2,
    max_receivers: Optional[int] = None,
    pos_cluster_thr: float = 80.0,
    spd_cluster_thr: float = 5.0,
    time_cluster_thr: float = 0.2,
    progress_every: int = 50,
) -> Dict[str, int]:
    """Process every receiver trace in *run_dir*, append episodes to *out_path*."""
    gt_files = list(run_dir.glob("traceGroundTruthJSON-*.json"))
    gt_info = None
    if gt_files:
        print(f"  Loading ground truth: {gt_files[0].name} ...", flush=True)
        gt_info = load_ground_truth(gt_files[0])
        n_pseudo = len(gt_info["pseudo_to_sender"])
        n_sybil = len(gt_info["sybil_senders"])
        print(f"  GT ready: {n_pseudo} pseudo->sender, {n_sybil} Sybil senders", flush=True)

    traces = sorted(run_dir.glob("traceJSON-*.json"))
    if max_receivers is not None:
        traces = traces[:max_receivers]

    run_id = f"{run_dir.parent.name}/{run_dir.name}"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    stats = {
        "receivers_processed": 0,
        "episodes_written": 0,
        "positive_episodes": 0,
    }

    with open(out_path, "a", encoding="utf-8") as fout:
        for i, trace_file in enumerate(traces, start=1):
            episodes = episodes_from_receiver(
                trace_file,
                run_id,
                attack_type=attack_type,
                source_folder=run_dir.parent.name,
                traffic_regime=traffic_regime,
                gt_info=gt_info,
                window_sec=window_sec,
                step_sec=step_sec,
                precision=precision,
                pos_cluster_thr=pos_cluster_thr,
                spd_cluster_thr=spd_cluster_thr,
                time_cluster_thr=time_cluster_thr,
            )
            for episode in episodes:
                fout.write(json.dumps(episode, ensure_ascii=False) + "\n")
            stats["receivers_processed"] += 1
            stats["episodes_written"] += len(episodes)
            stats["positive_episodes"] += sum(
                1 for ep in episodes if ep["label"]["contains_sybil_attacker"]
            )

            if i % progress_every == 0 or i == len(traces):
                print(
                    f"    [{i}/{len(traces)}] receivers done | "
                    f"episodes={stats['episodes_written']} | "
                    f"positives={stats['positive_episodes']}",
                    flush=True,
                )

    return stats


# ── CLI ──────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Build episode-level JSONL from VeReMi GridSybil traceJSON files.",
    )
    ap.add_argument("--raw_dir", type=str, required=True, help="Attack-type folder, e.g. data/raw/GridSybil_0709")
    ap.add_argument("--out_dir", type=str, default="data/processed/episodes")
    ap.add_argument("--window_sec", type=float, default=10.0)
    ap.add_argument("--step_sec", type=float, default=5.0)
    ap.add_argument("--precision", type=int, default=2)
    ap.add_argument("--max_receivers", type=int, default=None, help="Cap receivers per run for quick debugging")
    ap.add_argument("--pos_cluster_thr", type=float, default=80.0)
    ap.add_argument("--spd_cluster_thr", type=float, default=5.0)
    ap.add_argument("--time_cluster_thr", type=float, default=0.2)
    ap.add_argument("--progress_every", type=int, default=50)
    args = ap.parse_args()

    raw_dir = Path(args.raw_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    run_dirs = sorted(d for d in raw_dir.iterdir() if d.is_dir() and d.name.startswith("VeReMi_"))
    if not run_dirs:
        print(f"No VeReMi_* directories in {raw_dir}")
        return

    attack_type = raw_dir.name
    traffic_regime = _traffic_regime_from_attack_type(attack_type)
    print(
        f"Attack type: {attack_type} | regime: {traffic_regime} | runs: {len(run_dirs)}",
        flush=True,
    )

    grand_receivers = 0
    grand_episodes = 0
    grand_positives = 0
    for run_idx, run_dir in enumerate(run_dirs, start=1):
        out_file = out_dir / f"{attack_type}_{run_dir.name}.jsonl"
        print(f"\n>> Run {run_idx}/{len(run_dirs)}: {run_dir.name}", flush=True)
        print(f"   Output: {out_file}", flush=True)
        stats = process_run(
            run_dir,
            out_file,
            attack_type=attack_type,
            traffic_regime=traffic_regime,
            window_sec=args.window_sec,
            step_sec=args.step_sec,
            precision=args.precision,
            max_receivers=args.max_receivers,
            pos_cluster_thr=args.pos_cluster_thr,
            spd_cluster_thr=args.spd_cluster_thr,
            time_cluster_thr=args.time_cluster_thr,
            progress_every=args.progress_every,
        )
        grand_receivers += stats["receivers_processed"]
        grand_episodes += stats["episodes_written"]
        grand_positives += stats["positive_episodes"]
        print(
            f"   Done: receivers={stats['receivers_processed']} | "
            f"episodes={stats['episodes_written']} | "
            f"positives={stats['positive_episodes']}",
            flush=True,
        )

    print(
        f"\nAll done. receivers={grand_receivers} | "
        f"episodes={grand_episodes} | positives={grand_positives}",
        flush=True,
    )


if __name__ == "__main__":
    main()
