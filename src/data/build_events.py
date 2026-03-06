from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple, Optional

import numpy as np
import pandas as pd


# -------------------------
# Helpers
# -------------------------

@dataclass
class EgoStream:
    t: np.ndarray          # shape (N,)
    pos: np.ndarray        # shape (N, 3)
    spd: np.ndarray        # shape (N, 3)

def read_vehicle_jsonl(path: Path) -> Tuple[List[dict], List[dict]]:
    """Return (type2_records, type3_records) as lists of dicts."""
    type2 = []
    type3 = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            t = obj.get("type", None)
            if t == 2:
                type2.append(obj)
            elif t == 3:
                type3.append(obj)
    return type2, type3

def ego_stream_from_type2(type2: List[dict]) -> EgoStream:
    # Sort by rcvTime
    type2_sorted = sorted(type2, key=lambda x: x["rcvTime"])
    t = np.array([x["rcvTime"] for x in type2_sorted], dtype=np.float64)
    pos = np.array([x["pos"] for x in type2_sorted], dtype=np.float64)
    spd = np.array([x["spd"] for x in type2_sorted], dtype=np.float64)
    return EgoStream(t=t, pos=pos, spd=spd)

def nearest_index(t_arr: np.ndarray, t: float) -> int:
    """Return index of nearest time in sorted array."""
    # np.searchsorted gives insertion point
    i = int(np.searchsorted(t_arr, t))
    if i <= 0:
        return 0
    if i >= len(t_arr):
        return len(t_arr) - 1
    # choose closer of i-1 and i
    if abs(t_arr[i] - t) < abs(t_arr[i-1] - t):
        return i
    return i - 1

def vec_norm_xy(v: np.ndarray) -> float:
    return float(math.hypot(v[0], v[1]))

def compute_eps(values: np.ndarray, q: float) -> float:
    """Robust quantile with fallback."""
    if len(values) == 0:
        return 0.0
    return float(np.quantile(values, q))


# -------------------------
# Core: parse one run
# -------------------------

def parse_run_to_events(run_dir: Path, run_id: str) -> pd.DataFrame:
    veh_dir = run_dir / "vehicles"
    veh_files = sorted(list(veh_dir.glob("JSONlog-*.json")))
    if not veh_files:
        raise FileNotFoundError(f"No vehicle logs in {veh_dir}")

    # Build veh_id -> path and is_attacker from filename suffix "A0/A1"
    veh_path: Dict[int, Path] = {}
    veh_is_attacker: Dict[int, int] = {}

    # filename example: JSONlog-a-b-Ac.json  (a=index, b=veh_id, c in {0,1})
    for p in veh_files:
        name = p.name
        try:
            # Split by '-' then parse
            # JSONlog, a, b, Ac.json
            parts = name.replace(".json", "").split("-")
            veh_id = int(parts[2])
            a_part = parts[3]  # e.g., "A0" or "A1"
            is_att = int(a_part[1:])
            veh_path[veh_id] = p
            veh_is_attacker[veh_id] = is_att
        except Exception:
            # If a filename doesn't match, ignore it (manifest should have warned)
            continue

    if not veh_path:
        raise RuntimeError(f"Could not parse veh_id from filenames in {veh_dir}")

    # Pass 1: build ego stream for every vehicle (type=2)
    ego_streams: Dict[int, EgoStream] = {}
    rx_msgs_by_receiver: Dict[int, List[dict]] = {}

    for vid, p in veh_path.items():
        type2, type3 = read_vehicle_jsonl(p)
        ego_streams[vid] = ego_stream_from_type2(type2)
        rx_msgs_by_receiver[vid] = type3  # type=3 received by this receiver

    # Pass 2: build event rows
    rows: List[dict] = []
    missing_sender_truth = 0

    for receiver_id, msgs in rx_msgs_by_receiver.items():
        ego_r = ego_streams.get(receiver_id)
        if ego_r is None or len(ego_r.t) == 0:
            continue

        for m in msgs:
            sender_id = int(m["sender"])
            send_time = float(m.get("sendTime", m["rcvTime"]))
            rcv_time = float(m["rcvTime"])

            # receiver ego aligned to rcvTime
            ir = nearest_index(ego_r.t, rcv_time)
            ego_pos = ego_r.pos[ir]
            ego_spd = ego_r.spd[ir]

            # sender truth aligned to sendTime
            ego_s = ego_streams.get(sender_id)
            if ego_s is None or len(ego_s.t) == 0:
                missing_sender_truth += 1
                continue
            is_ = nearest_index(ego_s.t, send_time)
            snd_pos_true = ego_s.pos[is_]
            snd_spd_true = ego_s.spd[is_]

            claimed_pos = np.array(m["pos"], dtype=np.float64)
            claimed_spd = np.array(m["spd"], dtype=np.float64)

            err_pos = vec_norm_xy(claimed_pos - snd_pos_true)
            err_spd = vec_norm_xy(claimed_spd - snd_spd_true)

            rows.append({
                "run_id": run_id,
                "receiver_id": receiver_id,
                "sender_id": sender_id,
                "receiver_is_attacker": int(veh_is_attacker.get(receiver_id, 0)),
                "sender_is_attacker": int(veh_is_attacker.get(sender_id, 0)),

                "rcvTime": rcv_time,
                "sendTime": send_time,
                "messageID": int(m.get("messageID", -1)),
                "RSSI": float(m.get("RSSI", np.nan)),

                # claimed (observable)
                "pos_x": float(claimed_pos[0]),
                "pos_y": float(claimed_pos[1]),
                "spd_x": float(claimed_spd[0]),
                "spd_y": float(claimed_spd[1]),

                # receiver ego (observable)
                "ego_pos_x": float(ego_pos[0]),
                "ego_pos_y": float(ego_pos[1]),
                "ego_spd_x": float(ego_spd[0]),
                "ego_spd_y": float(ego_spd[1]),

                # sender truth (NOT for model input; only for labeling / analysis)
                "snd_pos_x_true": float(snd_pos_true[0]),
                "snd_pos_y_true": float(snd_pos_true[1]),
                "snd_spd_x_true": float(snd_spd_true[0]),
                "snd_spd_y_true": float(snd_spd_true[1]),

                "err_pos": float(err_pos),
                "err_spd": float(err_spd),
            })

    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError(f"No event rows created for run {run_id}. Missing sender truth? {missing_sender_truth}")

    # Auto thresholds based on benign senders (sender_is_attacker==0)
    benign = df[df["sender_is_attacker"] == 0]
    eps_pos = compute_eps(benign["err_pos"].to_numpy(dtype=np.float64), 0.999)
    eps_spd = compute_eps(benign["err_spd"].to_numpy(dtype=np.float64), 0.999)

    # Label
    df["attack_msg"] = ((df["err_pos"] > eps_pos) | (df["err_spd"] > eps_spd)).astype(int)
    df["eps_pos"] = eps_pos
    df["eps_spd"] = eps_spd
    df["missing_sender_truth_msgs"] = missing_sender_truth

    return df


# -------------------------
# CLI
# -------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--extracted_dir", type=str, required=True, help="e.g., data/extracted")
    ap.add_argument("--out_dir", type=str, default="data/processed/events")
    ap.add_argument("--max_runs", type=int, default=0, help="0=all")
    ap.add_argument("--only_missing", action="store_true", help="skip runs already processed")
    args = ap.parse_args()

    extracted_dir = Path(args.extracted_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    run_dirs = sorted([p for p in extracted_dir.iterdir() if p.is_dir()])
    if args.max_runs and args.max_runs > 0:
        run_dirs = run_dirs[: args.max_runs]

    ok, skipped, failed = 0, 0, 0
    for run_dir in run_dirs:
        run_id = run_dir.name
        out_path = out_dir / f"{run_id}.parquet"

        if args.only_missing and out_path.exists():
            skipped += 1
            continue

        try:
            df = parse_run_to_events(run_dir, run_id)
            df.to_parquet(out_path, index=False)
            ok += 1
            print(f"[OK] {run_id} -> {out_path.name}  rows={len(df)}  attack_rate={df['attack_msg'].mean():.4f}")
        except Exception as e:
            failed += 1
            print(f"[FAIL] {run_id}  error={repr(e)}")

    print(f"Done. ok={ok} skipped={skipped} failed={failed}")


if __name__ == "__main__":
    main()