from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List

import pandas as pd


def format_window(df_win: pd.DataFrame) -> Dict:
    """
    df_win: 已按 rcvTime 排序的窗口（长度K），最后一行是当前要判断的消息
    只输出可观测字段，避免泄漏 snd_true/err。
    """
    rows = []
    t_last = float(df_win["rcvTime"].iloc[-1])
    for _, r in df_win.iterrows():
        rows.append({
            "dt": float(r["rcvTime"]) - t_last,  # 相对时间（秒）
            "claimed": {
                "pos": [float(r["pos_x"]), float(r["pos_y"])],
                "spd": [float(r["spd_x"]), float(r["spd_y"])],
                "rssi": None if pd.isna(r["RSSI"]) else float(r["RSSI"]),
            },
            "ego": {
                "pos": [float(r["ego_pos_x"]), float(r["ego_pos_y"])],
                "spd": [float(r["ego_spd_x"]), float(r["ego_spd_y"])],
            }
        })
    return {"history": rows}


def make_jsonl_for_split(
    split_name: str,
    run_ids: List[str],
    events_dir: Path,
    out_path: Path,
    K: int,
    step: int,
    positive_keep: float,
    seed: int,
):
    """
    - step: 每隔 step 个窗口采样一次（降量）
    - positive_keep: 对 attack 样本保留比例（通常 1.0，全保留）
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

            # group by (receiver, sender)
            df = df.sort_values(["receiver_id", "sender_id", "rcvTime"])
            for (rcv, snd), g in df.groupby(["receiver_id", "sender_id"], sort=False):
                if len(g) < K:
                    continue
                g = g.reset_index(drop=True)

                # 滑窗
                for end in range(K - 1, len(g), step):
                    win = g.iloc[end - K + 1 : end + 1]
                    y = int(win["attack_msg"].iloc[-1])

                    # 降采样（可选）：这里只对正样本做 keep 比例控制
                    if y == 1 and positive_keep < 1.0:
                        if rng.random() > positive_keep:
                            continue

                    sample = {
                        "id": f"{run_id}|rcv{int(rcv)}|snd{int(snd)}|t{float(win['rcvTime'].iloc[-1]):.3f}",
                        "instruction": "You are an onboard CAV intrusion detection system. Output ONLY one label: BENIGN or ATTACK.",
                        "input": {
                            "run_id": run_id,
                            "receiver_id": int(rcv),
                            "sender_id": int(snd),
                            **format_window(win),
                        },
                        "output": "ATTACK" if y == 1 else "BENIGN",
                    }
                    f_out.write(json.dumps(sample) + "\n")
                    n_written += 1

    print(f"[{split_name}] wrote {n_written} samples -> {out_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split_json", type=str, default="data/processed/splits/split_v1.json")
    ap.add_argument("--events_dir", type=str, default="data/processed/events")
    ap.add_argument("--out_dir", type=str, default="data/processed/jsonl")
    ap.add_argument("--K", type=int, default=10)
    ap.add_argument("--step", type=int, default=1, help="sample every N windows to reduce size")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--positive_keep", type=float, default=1.0, help="keep ratio for ATTACK samples (usually 1.0)")
    args = ap.parse_args()

    split = json.loads(Path(args.split_json).read_text())
    runs = split["runs"]

    events_dir = Path(args.events_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    make_jsonl_for_split(
        "train", runs["train"], events_dir, out_dir / "train.jsonl",
        K=args.K, step=args.step, positive_keep=args.positive_keep, seed=args.seed
    )
    make_jsonl_for_split(
        "val", runs["val"], events_dir, out_dir / "val.jsonl",
        K=args.K, step=args.step, positive_keep=args.positive_keep, seed=args.seed
    )
    make_jsonl_for_split(
        "test", runs["test"], events_dir, out_dir / "test.jsonl",
        K=args.K, step=args.step, positive_keep=args.positive_keep, seed=args.seed
    )


if __name__ == "__main__":
    main()