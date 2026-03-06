from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List

import pandas as pd


def format_window(df_win: pd.DataFrame) -> Dict:
    """
    将一个按时间排序的窗口 DataFrame 转成可观测的时序输入。
    """
    rows = []
    t_last = float(df_win["rcvTime"].iloc[-1])
    for _, r in df_win.iterrows():
        rows.append(
            {
                "dt": float(r["rcvTime"]) - t_last,
                "sender_id": int(r["sender_id"]),
                "claimed": {
                    "pos": [float(r["pos_x"]), float(r["pos_y"])],
                    "spd": [float(r["spd_x"]), float(r["spd_y"])],
                    "rssi": None if pd.isna(r["RSSI"]) else float(r["RSSI"]),
                },
                "ego": {
                    "pos": [float(r["ego_pos_x"]), float(r["ego_pos_y"])],
                    "spd": [float(r["ego_spd_x"]), float(r["ego_spd_y"])],
                },
            }
        )
    return {"history": rows}


def iter_windows_phase1(df: pd.DataFrame, K: int, step: int):
    """
    Phase 1：按 (receiver_id, sender_id) 分组的单 sender 视角窗口。
    """
    df = df.sort_values(["receiver_id", "sender_id", "rcvTime"])
    for (rcv, snd), g in df.groupby(["receiver_id", "sender_id"], sort=False):
        if len(g) < K:
            continue
        g = g.reset_index(drop=True)
        for end in range(K - 1, len(g), step):
            win = g.iloc[end - K + 1 : end + 1]
            yield int(rcv), int(snd), win


def iter_windows_phase2(df: pd.DataFrame, K: int, step: int):
    """
    Phase 2：按 receiver_id 混流的视角窗口。
    窗口内可包含多个 sender，目标消息为窗口最后一条。
    """
    df = df.sort_values(["receiver_id", "rcvTime"])
    for rcv, g in df.groupby(["receiver_id"], sort=False):
        if len(g) < K:
            continue
        g = g.reset_index(drop=True)
        for end in range(K - 1, len(g), step):
            win = g.iloc[end - K + 1 : end + 1]
            snd = int(win["sender_id"].iloc[-1])
            yield int(rcv), snd, win


def make_label(y: int, run_id: str, attacker_type_map: Dict[str, object], label_mode: str) -> str:
    """
    根据 label_mode 生成输出标签。
    - binary: BENIGN / ATTACK
    - multiclass: BENIGN / ATTACK_TYPE_<attacker_type>
    """
    if label_mode == "binary":
        return "ATTACK" if y == 1 else "BENIGN"

    # multiclass
    if y == 0:
        return "BENIGN"

    atk_type = attacker_type_map.get(run_id, None)
    if atk_type is None or pd.isna(atk_type):
        return "ATTACK"
    return f"ATTACK_TYPE_{int(atk_type)}"


def make_jsonl_for_split(
    split_name: str,
    run_ids: List[str],
    events_dir: Path,
    out_path: Path,
    K: int,
    step: int,
    positive_keep: float,
    seed: int,
    phase: str,
    label_mode: str,
    attacker_type_map: Dict[str, object],
):
    """
    - step: 每隔 step 个窗口采样一次（降量）
    - positive_keep: 对 attack 样本保留比例（通常 1.0，全保留）
    - phase: 'phase1' or 'phase2'
    - label_mode: 'binary' or 'multiclass'
    """
    import numpy as np

    rng = np.random.default_rng(seed)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    n_written = 0

    with out_path.open("w", encoding="utf-8") as f_out:
        for run_id in run_ids:
            p = events_dir / f"{run_id}.parquet"
            if not p.exists():
                continue
            df = pd.read_parquet(p)

            if phase == "phase1":
                window_iter = iter_windows_phase1(df, K, step)
            elif phase == "phase2":
                window_iter = iter_windows_phase2(df, K, step)
            else:
                raise ValueError(f"Unknown phase: {phase}")

            for rcv, snd, win in window_iter:
                y = int(win["attack_msg"].iloc[-1])

                # 可选：对正样本做降采样
                if y == 1 and positive_keep < 1.0:
                    if rng.random() > positive_keep:
                        continue

                label = make_label(y, run_id, attacker_type_map, label_mode)

                sample = {
                    "id": f"{run_id}|rcv{rcv}|snd{snd}|t{float(win['rcvTime'].iloc[-1]):.3f}",
                    "instruction": "You are an onboard CAV intrusion detection system. Output ONLY one label.",
                    "input": {
                        "phase": phase,
                        "run_id": run_id,
                        "receiver_id": rcv,
                        "sender_id": snd,
                        "attacker_type": None
                        if run_id not in attacker_type_map or pd.isna(attacker_type_map[run_id])
                        else int(attacker_type_map[run_id]),
                        **format_window(win),
                    },
                    "output": label,
                }
                f_out.write(json.dumps(sample) + "\n")
                n_written += 1

    print(f"[{split_name}] wrote {n_written} samples -> {out_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split_json", type=str, default="data/processed/splits/split_v1.json")
    ap.add_argument("--events_dir", type=str, default="data/processed/events")
    ap.add_argument("--out_dir", type=str, default="data/processed/jsonl")
    ap.add_argument("--runs_csv", type=str, default="data/processed/manifests/runs.csv")
    ap.add_argument("--phase", type=str, choices=["phase1", "phase2"], default="phase1")
    ap.add_argument("--label_mode", type=str, choices=["binary", "multiclass"], default="binary")
    ap.add_argument("--task_name", type=str, default=None)
    ap.add_argument("--K", type=int, default=10)
    ap.add_argument("--step", type=int, default=1, help="sample every N windows to reduce size")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument(
        "--positive_keep",
        type=float,
        default=1.0,
        help="keep ratio for ATTACK samples (usually 1.0)",
    )
    args = ap.parse_args()

    split = json.loads(Path(args.split_json).read_text())
    runs = split["runs"]

    events_dir = Path(args.events_dir)

    # 读取 runs.csv 获取 run_id -> attacker_type 映射（用于多分类标签）
    runs_df = pd.read_csv(args.runs_csv)
    attacker_type_map: Dict[str, object] = {
        str(r["run_id"]): r.get("attacker_type", None) for _, r in runs_df.iterrows()
    }

    task_name = args.task_name or f"{args.phase}_{args.label_mode}"
    base_out_dir = Path(args.out_dir) / task_name
    base_out_dir.mkdir(parents=True, exist_ok=True)

    make_jsonl_for_split(
        "train",
        runs["train"],
        events_dir,
        base_out_dir / "train.jsonl",
        K=args.K,
        step=args.step,
        positive_keep=args.positive_keep,
        seed=args.seed,
        phase=args.phase,
        label_mode=args.label_mode,
        attacker_type_map=attacker_type_map,
    )
    make_jsonl_for_split(
        "val",
        runs["val"],
        events_dir,
        base_out_dir / "val.jsonl",
        K=args.K,
        step=args.step,
        positive_keep=args.positive_keep,
        seed=args.seed,
        phase=args.phase,
        label_mode=args.label_mode,
        attacker_type_map=attacker_type_map,
    )
    make_jsonl_for_split(
        "test",
        runs["test"],
        events_dir,
        base_out_dir / "test.jsonl",
        K=args.K,
        step=args.step,
        positive_keep=args.positive_keep,
        seed=args.seed,
        phase=args.phase,
        label_mode=args.label_mode,
        attacker_type_map=attacker_type_map,
    )


if __name__ == "__main__":
    main()