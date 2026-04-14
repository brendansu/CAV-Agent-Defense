from __future__ import annotations

import argparse
import glob
import json
import math
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple

try:
    import pandas as pd  # type: ignore
except Exception:  # pragma: no cover
    pd = None

try:
    import pyarrow as pa  # type: ignore
    import pyarrow.parquet as pq  # type: ignore
except Exception:  # pragma: no cover
    pa = None
    pq = None


SCHEMA_VERSION = "a_prime_v1"


F2MD_DEFAULTS: Dict[str, float] = {
    "MAX_PROXIMITY_RANGE_L": 30.0,
    "MAX_PROXIMITY_RANGE_W": 3.0,
    "MAX_PROXIMITY_DISTANCE": 2.0,
    "MAX_PLAUSIBLE_RANGE": 420.0,
    "MAX_TIME_DELTA": 3.1,
    "MAX_DELTA_INTER": 2.0,
    "MAX_SA_RANGE": 420.0,
    "MAX_SA_TIME": 2.1,
    "MIN_MAX_SPEED": 40.0,
    "MIN_MAX_ACCEL": 3.0,
    "MIN_MAX_DECEL": 4.5,
    "MAX_MGT_RNG": 4.0,
    "MAX_MGT_RNG_DOWN": 6.2,
    "MAX_MGT_RNG_UP": 2.1,
    "MAX_BEACON_FREQUENCY": 0.9,
    "MAX_HEADING_CHANGE": 90.0,
    "POS_HEADING_TIME": 1.1,
}


@dataclass
class BuildConfig:
    input_glob: str
    output_dir: str
    meta_dir: str
    output_format: str
    context_window_sec: float
    context_radius_m: float
    neighbor_cap: int
    vehicle_width_m: float
    vehicle_length_m: float
    min_conf_eps: float
    sim_dist_thresh_m: float
    sim_speed_thresh_mps: float
    sim_heading_thresh_deg: float
    workers: int


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _hypot2(x: float, y: float) -> float:
    return float(math.hypot(x, y))


def _speed_from_vec(v: List[float]) -> float:
    return _hypot2(float(v[0]), float(v[1]))


def _heading_deg(hed: List[float]) -> float:
    return math.degrees(math.atan2(float(hed[1]), float(hed[0])))


def _heading_from_points(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    return math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))


def _angle_diff_deg(a: float, b: float) -> float:
    d = abs(a - b) % 360.0
    return 360.0 - d if d > 180.0 else d


def _dist(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    return _hypot2(a[0] - b[0], a[1] - b[1])


def _safe_mean(xs: List[float]) -> float:
    return float(sum(xs) / len(xs)) if xs else math.nan


def _safe_std(xs: List[float]) -> float:
    if len(xs) < 2:
        return math.nan
    mu = sum(xs) / len(xs)
    return math.sqrt(sum((x - mu) ** 2 for x in xs) / len(xs))


def _safe_min(xs: List[float]) -> float:
    return min(xs) if xs else math.nan


def _safe_p10(xs: List[float]) -> float:
    if not xs:
        return math.nan
    s = sorted(xs)
    idx = int(round(0.1 * (len(s) - 1)))
    return float(s[idx])


def _parse_trace_filename(path: Path) -> Dict[str, int]:
    # traceJSON-<vid>-<nid>-A<at>-<sec>-<run>.json
    stem = path.stem
    parts = stem.split("-")
    if len(parts) < 6 or parts[0] != "traceJSON":
        raise ValueError(f"Unexpected trace filename: {path.name}")
    return {
        "receiver_id": int(parts[1]),
        "neighbor_id": int(parts[2]),
        "attack_flag": int(parts[3].lstrip("A")),
        "start_sec": int(parts[4]),
        "run_idx": int(parts[5]),
    }


def _episode_id_from_path(path: Path) -> str:
    parent = path.parent.name  # VeReMi_...
    attack_type = path.parent.parent.name  # GridSybil_...
    return f"{attack_type}/{parent}"


def _linear_overlap_score(value: float, bound: float, tol: float) -> float:
    """Soft threshold score in [0,1], high is plausible."""
    tol = max(tol, 1e-9)
    if value <= bound - tol:
        return 1.0
    if value >= bound + tol:
        return 0.0
    return (bound + tol - value) / (2.0 * tol)


def _circle_intersection_area(r1: float, r2: float, d: float) -> float:
    """Area of intersection between two circles."""
    if r1 <= 0 or r2 <= 0:
        return 0.0
    if d >= r1 + r2:
        return 0.0
    if d <= abs(r1 - r2):
        return math.pi * min(r1, r2) ** 2

    r1_2 = r1 * r1
    r2_2 = r2 * r2
    alpha = math.acos(_clamp((d * d + r1_2 - r2_2) / (2 * d * r1), -1.0, 1.0))
    beta = math.acos(_clamp((d * d + r2_2 - r1_2) / (2 * d * r2), -1.0, 1.0))
    area = r1_2 * alpha + r2_2 * beta
    area -= 0.5 * math.sqrt(
        _clamp(
            (-d + r1 + r2) * (d + r1 - r2) * (d - r1 + r2) * (d + r1 + r2),
            0.0,
            float("inf"),
        )
    )
    return area


def _range_plausibility(d: float, sender_conf: float, receiver_conf: float, max_range: float) -> float:
    tol = sender_conf + receiver_conf
    return _linear_overlap_score(d, max_range, tol)


def _speed_plausibility(speed: float, speed_conf: float, max_speed: float) -> float:
    c = max(abs(speed_conf), 1e-9)
    s = abs(speed)
    if s + c / 2.0 < max_speed:
        return 1.0
    if s - c / 2.0 > max_speed:
        return 0.0
    return _clamp((c / 2.0 + (max_speed - s)) / c, 0.0, 1.0)


def _position_consistency(
    cur_pos: Tuple[float, float],
    cur_conf: float,
    old_pos: Tuple[float, float],
    old_conf: float,
    dt: float,
    max_speed: float,
) -> float:
    if dt <= 0:
        return math.nan
    d = _dist(cur_pos, old_pos)
    bound = max_speed * dt
    tol = cur_conf + old_conf
    return _linear_overlap_score(d, bound, tol)


def _speed_consistency(
    cur_speed: float,
    cur_conf: float,
    old_speed: float,
    old_conf: float,
    dt: float,
    max_accel: float,
    max_decel: float,
) -> float:
    if dt <= 0:
        return math.nan
    delta = cur_speed - old_speed
    bound = (max_accel if delta > 0 else max_decel) * dt
    tol = cur_conf + old_conf
    return _linear_overlap_score(abs(delta), bound, tol)


def _pos_speed_consistency(
    cur_pos: Tuple[float, float],
    cur_conf: float,
    old_pos: Tuple[float, float],
    old_conf: float,
    cur_speed: float,
    cur_speed_conf: float,
    old_speed: float,
    old_speed_conf: float,
    dt: float,
    max_accel: float,
    max_decel: float,
    max_mgt_rng_up: float,
) -> float:
    if dt <= 0:
        return math.nan
    d = _dist(cur_pos, old_pos)
    pred_d = 0.5 * (cur_speed + old_speed) * dt
    accel_term = 0.5 * max(max_accel, max_decel) * dt * dt
    tol = cur_conf + old_conf + cur_speed_conf * dt + old_speed_conf * dt + max_mgt_rng_up
    denom = max(accel_term + tol, 1e-6)
    return _clamp(1.0 - abs(d - pred_d) / denom, 0.0, 1.0)


def _pos_speed_max_consistency(
    cur_pos: Tuple[float, float],
    cur_conf: float,
    old_pos: Tuple[float, float],
    old_conf: float,
    cur_speed: float,
    cur_speed_conf: float,
    old_speed: float,
    old_speed_conf: float,
    dt: float,
    max_accel: float,
    max_decel: float,
    max_mgt_rng: float,
) -> float:
    if dt <= 0:
        return math.nan
    d = _dist(cur_pos, old_pos)
    theo_speed = d / dt
    maxspeed = max(cur_speed, old_speed)
    minspeed = min(cur_speed, old_speed)
    tol = cur_conf / dt + old_conf / dt + cur_speed_conf + old_speed_conf + max_mgt_rng

    upper_bound = maxspeed + (max_decel + max_mgt_rng) * dt
    lower_bound = max(minspeed - (max_accel + max_mgt_rng) * dt, 0.0)

    upper_score = _linear_overlap_score(max(theo_speed - upper_bound, 0.0), 0.0, tol)
    lower_score = _linear_overlap_score(max(lower_bound - theo_speed, 0.0), 0.0, tol)
    return min(upper_score, lower_score)


def _beacon_frequency(dt: float, min_period: float) -> float:
    if dt <= 0:
        return math.nan
    return 0.0 if dt < min_period else 1.0


def _position_heading_consistency(
    cur_heading_deg: float,
    cur_heading_conf_deg: float,
    old_pos: Tuple[float, float],
    cur_pos: Tuple[float, float],
    dt: float,
    cur_speed: float,
    cur_speed_conf: float,
    max_heading_change_deg: float,
    pos_heading_time: float,
) -> float:
    if dt <= 0:
        return math.nan
    if dt >= pos_heading_time:
        return 1.0
    d = _dist(cur_pos, old_pos)
    if d < 1.0:
        return 1.0
    if cur_speed - cur_speed_conf < 1.0:
        return 1.0

    move_heading_deg = _heading_from_points(old_pos, cur_pos)
    delta = _angle_diff_deg(cur_heading_deg, move_heading_deg)
    allowance = max_heading_change_deg + cur_heading_conf_deg
    if delta <= allowance:
        return 1.0
    return _clamp(1.0 - (delta - allowance) / 90.0, 0.0, 1.0)


def _sudden_appearance(
    receiver_pos: Tuple[float, float],
    receiver_conf: float,
    sender_pos: Tuple[float, float],
    sender_conf: float,
    max_sa_range: float,
) -> float:
    d = _dist(sender_pos, receiver_pos)
    r2 = max_sa_range + receiver_conf

    if sender_conf <= 0:
        return 0.0 if d < r2 else 1.0

    inter = _circle_intersection_area(sender_conf, r2, d)
    area_sender = math.pi * sender_conf * sender_conf
    ratio = inter / max(area_sender, 1e-9)
    return _clamp(1.0 - ratio, 0.0, 1.0)


def _proximity_plausibility(
    sender_pos: Tuple[float, float],
    receiver_pos: Tuple[float, float],
    neighbor_positions: List[Tuple[float, float]],
    params: Dict[str, float],
) -> float:
    d = _dist(sender_pos, receiver_pos)
    rel = (sender_pos[0] - receiver_pos[0], sender_pos[1] - receiver_pos[1])
    angle = abs(math.degrees(math.atan2(rel[1], rel[0])))
    cond = d < params["MAX_PROXIMITY_RANGE_L"] and (
        d < params["MAX_PROXIMITY_RANGE_W"] * 2.0
        or (
            angle < 90.0
            and d < (params["MAX_PROXIMITY_RANGE_W"] / max(math.cos(math.radians(90.0 - angle)), 1e-6))
        )
    )
    if not cond:
        return 1.0

    if not neighbor_positions:
        return 0.0
    min_dist = min(_dist(sender_pos, p) for p in neighbor_positions)
    thr = 2.0 * params["MAX_PROXIMITY_DISTANCE"]
    if min_dist < thr:
        return _clamp(1.0 - min_dist / thr, 0.0, 1.0)
    return 0.0


def _intersection_plausibility(
    a_pos: Tuple[float, float],
    a_conf: float,
    b_pos: Tuple[float, float],
    b_conf: float,
    vehicle_width: float,
    vehicle_length: float,
) -> float:
    # Approximation: circles with radius from vehicle diagonal and confidence.
    base_r = 0.5 * math.hypot(vehicle_width, vehicle_length)
    ra = base_r + a_conf
    rb = base_r + b_conf
    d = _dist(a_pos, b_pos)
    overlap = max(0.0, ra + rb - d)
    if overlap <= 0:
        return 1.0
    risk = overlap / max(ra + rb, 1e-9)
    return _clamp(1.0 - risk, 0.0, 1.0)


def _iter_json_lines(path: Path) -> Iterable[Tuple[int, Dict[str, Any]]]:
    with path.open("r", encoding="utf-8") as f:
        for idx, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            yield idx, json.loads(line)


def _build_vehicle_attack_map(paths: List[Path]) -> Dict[int, int]:
    out: Dict[int, int] = {}
    for p in paths:
        meta = _parse_trace_filename(p)
        vid = meta["receiver_id"]
        flag = meta["attack_flag"]
        prev = out.get(vid)
        if prev is None:
            out[vid] = flag
        elif prev != flag:
            # Prefer non-zero attack flag when conflict occurs.
            out[vid] = max(prev, flag)
    return out


def _process_trace_file(
    path: Path,
    cfg: BuildConfig,
    params: Dict[str, float],
    vehicle_attack_map: Dict[int, int],
) -> List[Dict[str, Any]]:
    file_meta = _parse_trace_filename(path)
    receiver_id = file_meta["receiver_id"]
    episode_id = _episode_id_from_path(path)
    source_file = str(path).replace("\\", "/")

    rows: List[Dict[str, Any]] = []

    receiver_state: Optional[Dict[str, Any]] = None
    last_by_pseudo: Dict[int, Dict[str, Any]] = {}
    latest_by_pseudo: Dict[int, Dict[str, Any]] = {}
    recent_msgs: Deque[Dict[str, Any]] = deque()

    fresh_sec = max(cfg.context_window_sec, params["MAX_DELTA_INTER"])

    for line_idx, rec in _iter_json_lines(path):
        rec_type = rec.get("type")
        t = float(rec.get("rcvTime", rec.get("sendTime", -1.0)))
        if t < 0:
            continue

        if rec_type == 2:
            pos = rec.get("pos", [math.nan, math.nan, 0.0])
            pos_noise = rec.get("pos_noise", [0.0, 0.0, 0.0])
            hed = rec.get("hed", [1.0, 0.0, 0.0])
            hed_noise = rec.get("hed_noise", [0.0, 0.0, 0.0])
            receiver_state = {
                "t": t,
                "pos": (float(pos[0]), float(pos[1])),
                "pos_conf": max(_hypot2(float(pos_noise[0]), float(pos_noise[1])), cfg.min_conf_eps),
                "heading_deg": _heading_deg(hed),
                "heading_conf_deg": max(_hypot2(float(hed_noise[0]), float(hed_noise[1])), cfg.min_conf_eps),
            }
            continue

        if rec_type != 3:
            continue
        if receiver_state is None:
            # Skip type3 rows before the first ego state.
            continue

        # Causal context window [t-delta, t]
        t0 = t - cfg.context_window_sec
        while recent_msgs and recent_msgs[0]["t"] < t0:
            recent_msgs.popleft()

        sender = int(rec.get("sender", -1))
        pseudo = int(rec.get("senderPseudo", -1))
        message_id = int(rec.get("messageID", -1))
        send_time = float(rec.get("sendTime", t))

        pos = rec.get("pos", [math.nan, math.nan, 0.0])
        pos_noise = rec.get("pos_noise", [0.0, 0.0, 0.0])
        spd = rec.get("spd", [0.0, 0.0, 0.0])
        spd_noise = rec.get("spd_noise", [0.0, 0.0, 0.0])
        acl = rec.get("acl", [0.0, 0.0, 0.0])
        acl_noise = rec.get("acl_noise", [0.0, 0.0, 0.0])
        hed = rec.get("hed", [1.0, 0.0, 0.0])
        hed_noise = rec.get("hed_noise", [0.0, 0.0, 0.0])

        cur_pos = (float(pos[0]), float(pos[1]))
        cur_speed = _speed_from_vec(spd)
        cur_heading_deg = _heading_deg(hed)
        cur_pos_conf = max(_hypot2(float(pos_noise[0]), float(pos_noise[1])), cfg.min_conf_eps)
        cur_spd_conf = max(_hypot2(float(spd_noise[0]), float(spd_noise[1])), cfg.min_conf_eps)
        cur_acl_conf = max(_hypot2(float(acl_noise[0]), float(acl_noise[1])), cfg.min_conf_eps)
        cur_heading_conf_deg = max(_hypot2(float(hed_noise[0]), float(hed_noise[1])), cfg.min_conf_eps)

        prev = last_by_pseudo.get(pseudo)
        has_prev = int(prev is not None)
        dt_prev = float(t - prev["t"]) if prev is not None else math.nan

        # --- Catch-like checks ---
        art = _range_plausibility(
            _dist(cur_pos, receiver_state["pos"]),
            cur_pos_conf,
            receiver_state["pos_conf"],
            params["MAX_PLAUSIBLE_RANGE"],
        )
        maxs = _speed_plausibility(cur_speed, cur_spd_conf, params["MIN_MAX_SPEED"])

        mgtd = math.nan
        mgts = math.nan
        mgtsv = math.nan
        mgtsvm = math.nan
        freq = math.nan
        phc = math.nan
        saw = math.nan
        valid_history = 0

        if prev is not None and dt_prev > 0:
            valid_history = 1
            mgtd = _position_consistency(
                cur_pos,
                cur_pos_conf,
                prev["pos"],
                prev["pos_conf"],
                dt_prev,
                params["MIN_MAX_SPEED"],
            )
            mgts = _speed_consistency(
                cur_speed,
                cur_spd_conf,
                prev["speed"],
                prev["speed_conf"],
                dt_prev,
                params["MIN_MAX_ACCEL"],
                params["MIN_MAX_DECEL"],
            )
            if dt_prev < params["MAX_TIME_DELTA"]:
                mgtsv = _pos_speed_consistency(
                    cur_pos,
                    cur_pos_conf,
                    prev["pos"],
                    prev["pos_conf"],
                    cur_speed,
                    cur_spd_conf,
                    prev["speed"],
                    prev["speed_conf"],
                    dt_prev,
                    params["MIN_MAX_ACCEL"],
                    params["MIN_MAX_DECEL"],
                    params["MAX_MGT_RNG_UP"],
                )
                mgtsvm = _pos_speed_max_consistency(
                    cur_pos,
                    cur_pos_conf,
                    prev["pos"],
                    prev["pos_conf"],
                    cur_speed,
                    cur_spd_conf,
                    prev["speed"],
                    prev["speed_conf"],
                    dt_prev,
                    params["MIN_MAX_ACCEL"],
                    params["MIN_MAX_DECEL"],
                    params["MAX_MGT_RNG"],
                )
            else:
                mgtsv = 1.0
                mgtsvm = 1.0
            freq = _beacon_frequency(dt_prev, params["MAX_BEACON_FREQUENCY"])
            phc = _position_heading_consistency(
                cur_heading_deg,
                cur_heading_conf_deg,
                prev["pos"],
                cur_pos,
                dt_prev,
                cur_speed,
                cur_spd_conf,
                params["MAX_HEADING_CHANGE"],
                params["POS_HEADING_TIME"],
            )
            if dt_prev > params["MAX_SA_TIME"]:
                saw = _sudden_appearance(
                    receiver_state["pos"],
                    receiver_state["pos_conf"],
                    cur_pos,
                    cur_pos_conf,
                    params["MAX_SA_RANGE"],
                )
        else:
            saw = _sudden_appearance(
                receiver_state["pos"],
                receiver_state["pos_conf"],
                cur_pos,
                cur_pos_conf,
                params["MAX_SA_RANGE"],
            )

        # Fresh neighbors for RAD/INT, excluding same pseudo and stale entries.
        fresh_neighbors: List[Dict[str, Any]] = []
        for p, st in latest_by_pseudo.items():
            if p == pseudo:
                continue
            if t - st["t"] <= fresh_sec:
                fresh_neighbors.append(st)
        if len(fresh_neighbors) > cfg.neighbor_cap:
            fresh_neighbors = sorted(
                fresh_neighbors,
                key=lambda s: _dist(cur_pos, s["pos"]),
            )[: cfg.neighbor_cap]

        neighbor_positions = [n["pos"] for n in fresh_neighbors]
        rad = _proximity_plausibility(cur_pos, receiver_state["pos"], neighbor_positions, params)

        int_self = _intersection_plausibility(
            receiver_state["pos"],
            receiver_state["pos_conf"],
            cur_pos,
            cur_pos_conf,
            cfg.vehicle_width_m,
            cfg.vehicle_length_m,
        )
        int_neighbor_scores: List[float] = []
        for n in fresh_neighbors:
            if t - n["t"] > params["MAX_DELTA_INTER"]:
                continue
            int_neighbor_scores.append(
                _intersection_plausibility(
                    n["pos"],
                    n["pos_conf"],
                    cur_pos,
                    cur_pos_conf,
                    cfg.vehicle_width_m,
                    cfg.vehicle_length_m,
                )
            )
        int_min_neighbor = min(int_neighbor_scores) if int_neighbor_scores else math.nan
        int_n_violations = int(sum(1 for x in int_neighbor_scores if x < 0.5))

        # --- A' context features from recent window ---
        window_msgs = list(recent_msgs)
        latest_window_by_pseudo: Dict[int, Dict[str, Any]] = {}
        for m in window_msgs:
            latest_window_by_pseudo[m["pseudo"]] = m

        neighbor_window = [
            m
            for p, m in latest_window_by_pseudo.items()
            if p != pseudo and _dist(cur_pos, m["pos"]) <= cfg.context_radius_m
        ]
        if len(neighbor_window) > cfg.neighbor_cap:
            neighbor_window = sorted(
                neighbor_window,
                key=lambda x: _dist(cur_pos, x["pos"]),
            )[: cfg.neighbor_cap]

        dists = [_dist(cur_pos, n["pos"]) for n in neighbor_window]
        speed_diffs = [abs(cur_speed - n["speed"]) for n in neighbor_window]
        head_diffs = [_angle_diff_deg(cur_heading_deg, n["heading_deg"]) for n in neighbor_window]

        n_triplet = 0
        for d, dv, dh in zip(dists, speed_diffs, head_diffs):
            if d <= cfg.sim_dist_thresh_m and dv <= cfg.sim_speed_thresh_mps and dh <= cfg.sim_heading_thresh_deg:
                n_triplet += 1

        same_sender_msgs = [m for m in window_msgs if m["sender"] == sender]
        same_sender_pseudos = {m["pseudo"] for m in same_sender_msgs}
        same_sender_pseudos.add(pseudo)
        same_sender_sorted = sorted(same_sender_msgs, key=lambda x: (x["t"], x["message_id"]))
        switch_count = 0
        if same_sender_sorted:
            prev_p = same_sender_sorted[0]["pseudo"]
            for m in same_sender_sorted[1:]:
                if m["pseudo"] != prev_p:
                    switch_count += 1
                prev_p = m["pseudo"]

        # --- Row ---
        sender_attack_flag = vehicle_attack_map.get(sender, math.nan)
        receiver_attack_flag = vehicle_attack_map.get(receiver_id, file_meta["attack_flag"])

        row = {
            "schema_version": SCHEMA_VERSION,
            "episode_id": episode_id,
            "receiver_id": receiver_id,
            "sender_id": sender,
            "sender_pseudo": pseudo,
            "message_id": message_id,
            "send_time": send_time,
            "rcv_time": t,
            "source_file": source_file,
            "source_line_idx": line_idx,
            "receiver_attack_flag": receiver_attack_flag,
            "sender_attack_flag": sender_attack_flag,
            "receiver_pos_x": receiver_state["pos"][0],
            "receiver_pos_y": receiver_state["pos"][1],
            "receiver_pos_conf": receiver_state["pos_conf"],
            "receiver_heading_deg": receiver_state["heading_deg"],
            "receiver_heading_conf_deg": receiver_state["heading_conf_deg"],
            "msg_pos_x": cur_pos[0],
            "msg_pos_y": cur_pos[1],
            "msg_spd_x": float(spd[0]),
            "msg_spd_y": float(spd[1]),
            "msg_speed_norm": cur_speed,
            "msg_acl_x": float(acl[0]),
            "msg_acl_y": float(acl[1]),
            "msg_acl_norm": _speed_from_vec(acl),
            "msg_heading_deg": cur_heading_deg,
            "msg_head_x": float(hed[0]),
            "msg_head_y": float(hed[1]),
            "msg_pos_conf": cur_pos_conf,
            "msg_spd_conf": cur_spd_conf,
            "msg_acl_conf": cur_acl_conf,
            "msg_head_conf_deg": cur_heading_conf_deg,
            "msg_has_prev_same_pseudo": has_prev,
            "msg_dt_prev_same_pseudo": dt_prev,
            "msg_valid_history_features": valid_history,
            "msg_catch_art": art,
            "msg_catch_maxs": maxs,
            "msg_catch_mgtd": mgtd,
            "msg_catch_mgts": mgts,
            "msg_catch_mgtsv": mgtsv,
            "msg_catch_mgtsvm": mgtsvm,
            "msg_catch_freq": freq,
            "msg_catch_phc": phc,
            "msg_catch_saw": saw,
            "msg_catch_rad": rad,
            "msg_catch_int_self": int_self,
            "msg_catch_int_min_neighbor": int_min_neighbor,
            "msg_catch_int_n_violations": int_n_violations,
            "msg_catch_map": math.nan,
            "ctx_n_active_pseudo_dt": len(latest_window_by_pseudo),
            "ctx_n_neighbors": len(neighbor_window),
            "ctx_dist_min": _safe_min(dists),
            "ctx_dist_p10": _safe_p10(dists),
            "ctx_dist_mean": _safe_mean(dists),
            "ctx_dist_std": _safe_std(dists),
            "ctx_n_close_5m": int(sum(1 for d in dists if d <= 5.0)),
            "ctx_n_close_10m": int(sum(1 for d in dists if d <= 10.0)),
            "ctx_n_close_20m": int(sum(1 for d in dists if d <= 20.0)),
            "ctx_speed_diff_min": _safe_min(speed_diffs),
            "ctx_speed_diff_mean": _safe_mean(speed_diffs),
            "ctx_n_speed_diff_lt_0p5": int(sum(1 for dv in speed_diffs if dv <= 0.5)),
            "ctx_n_speed_diff_lt_1p0": int(sum(1 for dv in speed_diffs if dv <= 1.0)),
            "ctx_head_diff_min_deg": _safe_min(head_diffs),
            "ctx_head_diff_mean_deg": _safe_mean(head_diffs),
            "ctx_n_head_diff_lt_5deg": int(sum(1 for dh in head_diffs if dh <= 5.0)),
            "ctx_n_head_diff_lt_10deg": int(sum(1 for dh in head_diffs if dh <= 10.0)),
            "ctx_n_triplet_similar": n_triplet,
            "ctx_triplet_ratio": (
                float(n_triplet / len(neighbor_window)) if neighbor_window else math.nan
            ),
            "ctx_n_pseudo_per_sender_dt": len(same_sender_pseudos),
            "ctx_is_sender_multi_pseudo_now": int(len(same_sender_pseudos) > 1),
            "ctx_sender_pseudo_switch_count_w": switch_count,
            "avail_prev_same_pseudo": has_prev,
            "avail_neighbor_context": int(len(neighbor_window) > 0),
            "avail_map": 0,
            "avail_int": 1,
            "int_is_approx": 1,
            "feature_flags": "map=off,int=approx,kalman=off",
        }
        rows.append(row)

        snap = {
            "t": t,
            "sender": sender,
            "pseudo": pseudo,
            "message_id": message_id,
            "pos": cur_pos,
            "pos_conf": cur_pos_conf,
            "speed": cur_speed,
            "speed_conf": cur_spd_conf,
            "heading_deg": cur_heading_deg,
        }
        last_by_pseudo[pseudo] = snap
        latest_by_pseudo[pseudo] = snap
        recent_msgs.append(snap)

    return rows


def _resolve_input_paths(pattern: str) -> List[Path]:
    paths = [Path(p) for p in glob.glob(pattern, recursive=True)]
    files = [p for p in paths if p.is_file() and p.name.startswith("traceJSON-") and p.suffix == ".json"]
    return sorted(files)


def _write_output(rows: List[Dict[str, Any]], output_dir: Path, fmt: str) -> Tuple[Path, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    if fmt == "parquet":
        out = output_dir / "message_features.parquet"
        if pd is not None:
            df = pd.DataFrame(rows)
            df.to_parquet(out, index=False)
            return out, "parquet"
        if pa is not None and pq is not None:
            table = pa.Table.from_pylist(rows)
            pq.write_table(table, out)
            return out, "parquet"
        # Fallback when no parquet dependency is installed.
        fmt = "jsonl"

    if fmt == "jsonl":
        out = output_dir / "message_features.jsonl"
        with out.open("w", encoding="utf-8") as f:
            for rec in rows:
                f.write(json.dumps(_jsonable(rec), ensure_ascii=False) + "\n")
        return out, "jsonl"
    raise ValueError(f"Unsupported output format: {fmt}")


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonable(v) for v in obj]
    return obj


def _write_run_config(meta_dir: Path, cfg: BuildConfig, params: Dict[str, float], stats: Dict[str, Any]) -> Path:
    meta_dir.mkdir(parents=True, exist_ok=True)
    out = meta_dir / "run_config.json"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "config": asdict(cfg),
        "f2md_params": params,
        "stats": stats,
    }
    with out.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return out


def parse_args() -> BuildConfig:
    ap = argparse.ArgumentParser(description="Build GridSybil plausibility message-level features (A').")
    ap.add_argument(
        "--input-glob",
        type=str,
        default="data/raw/**/traceJSON-*.json",
        help="Glob to raw receiver trace files.",
    )
    ap.add_argument(
        "--output-dir",
        type=str,
        default="plausibility_messages",
        help="Output directory for message-level features.",
    )
    ap.add_argument(
        "--meta-dir",
        type=str,
        default="plausibility_meta",
        help="Directory to write run_config and metadata.",
    )
    ap.add_argument("--format", type=str, default="parquet", choices=["parquet", "jsonl"])
    ap.add_argument("--context-window-sec", type=float, default=1.0)
    ap.add_argument("--context-radius-m", type=float, default=30.0)
    ap.add_argument("--neighbor-cap", type=int, default=20)
    ap.add_argument("--vehicle-width-m", type=float, default=2.0)
    ap.add_argument("--vehicle-length-m", type=float, default=5.0)
    ap.add_argument("--min-conf-eps", type=float, default=1e-3)
    ap.add_argument("--sim-dist-thresh-m", type=float, default=10.0)
    ap.add_argument("--sim-speed-thresh-mps", type=float, default=0.5)
    ap.add_argument("--sim-heading-thresh-deg", type=float, default=10.0)
    ap.add_argument("--workers", type=int, default=1)
    args = ap.parse_args()
    return BuildConfig(
        input_glob=args.input_glob,
        output_dir=args.output_dir,
        meta_dir=args.meta_dir,
        output_format=args.format,
        context_window_sec=args.context_window_sec,
        context_radius_m=args.context_radius_m,
        neighbor_cap=args.neighbor_cap,
        vehicle_width_m=args.vehicle_width_m,
        vehicle_length_m=args.vehicle_length_m,
        min_conf_eps=args.min_conf_eps,
        sim_dist_thresh_m=args.sim_dist_thresh_m,
        sim_speed_thresh_mps=args.sim_speed_thresh_mps,
        sim_heading_thresh_deg=args.sim_heading_thresh_deg,
        workers=max(1, int(args.workers)),
    )


def main() -> None:
    cfg = parse_args()
    params = dict(F2MD_DEFAULTS)

    files = _resolve_input_paths(cfg.input_glob)
    if not files:
        raise FileNotFoundError(f"No traceJSON files matched: {cfg.input_glob}")

    vehicle_attack_map = _build_vehicle_attack_map(files)
    all_rows: List[Dict[str, Any]] = []

    print(f"Matched {len(files)} receiver traces.")
    if cfg.workers == 1:
        for i, path in enumerate(files, start=1):
            rows = _process_trace_file(path, cfg, params, vehicle_attack_map)
            all_rows.extend(rows)
            if i % 20 == 0 or i == len(files):
                print(f"[{i}/{len(files)}] files processed, rows={len(all_rows)}")
    else:
        with ThreadPoolExecutor(max_workers=cfg.workers) as ex:
            futs = {
                ex.submit(_process_trace_file, p, cfg, params, vehicle_attack_map): p
                for p in files
            }
            done = 0
            for fut in as_completed(futs):
                done += 1
                rows = fut.result()
                all_rows.extend(rows)
                if done % 20 == 0 or done == len(files):
                    print(f"[{done}/{len(files)}] files processed, rows={len(all_rows)}")

    if not all_rows:
        raise RuntimeError("No message rows were generated (check input files and type=3 records).")

    all_rows.sort(
        key=lambda r: (
            r.get("episode_id", ""),
            r.get("receiver_id", -1),
            r.get("rcv_time", -1.0),
            r.get("message_id", -1),
            r.get("sender_pseudo", -1),
        )
    )

    out_path, actual_format = _write_output(all_rows, Path(cfg.output_dir), cfg.output_format)
    unique_episodes = len({r["episode_id"] for r in all_rows})
    unique_receivers = len({r["receiver_id"] for r in all_rows})
    stats = {
        "num_files": len(files),
        "num_rows": int(len(all_rows)),
        "num_episodes": unique_episodes,
        "num_receivers": unique_receivers,
        "output_format_requested": cfg.output_format,
        "output_format_actual": actual_format,
        "output_path": str(out_path).replace("\\", "/"),
    }
    meta_path = _write_run_config(Path(cfg.meta_dir), cfg, params, stats)

    print(f"Done. rows={stats['num_rows']} episodes={stats['num_episodes']} receivers={stats['num_receivers']}")
    print(f"Features: {out_path}")
    print(f"Run config: {meta_path}")


if __name__ == "__main__":
    main()
