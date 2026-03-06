from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd


def split_runs_per_config(df: pd.DataFrame, seed: int = 42) -> Dict[str, List[str]]:
    rng = np.random.default_rng(seed)

    # 1. 只保留状态正常、字段不空的 run
    df = df.copy()
    df = df[df["status"] == "ok"]
    df = df.dropna(subset=["attacker_type", "start_h", "attacker_prob"])

    config_cols = ["attacker_type", "start_h", "attacker_prob"]

    train: List[str] = []
    pool: List[str] = []

    # 2. 对每个配置 (type, start_h, prob) 分组：5 个 run → 4 train + 1 pool
    for _, g in df.groupby(config_cols):
        run_ids = g["run_id"].tolist()
        # 理论上应该是 5 个，你可以加个检查：
        # if len(run_ids) != 5: 这里可以打印 warning 或者继续按当前数量处理
        rng.shuffle(run_ids)
        if len(run_ids) >= 4:
            train.extend(run_ids[:4])
            pool.extend(run_ids[4:])
        else:
            # 极端情况：这个配置少于 5 个 run，就全部放 train
            train.extend(run_ids)

    pool_df = df[df["run_id"].isin(pool)].copy()
    val: List[str] = []
    test: List[str] = []

    # 3. 在 pool 里，仅按 attacker_type 做分层；
    #    每个 attacker_type 理论上有 9 个 run，我们按照 4/5/4/5/4 分到 val，
    #    对应地 5/4/5/4/5 分到 test，保证：
    #      - 每个 attacker_type 在 val 和 test 中都出现
    #      - 全局大约 22 个 val、23 个 test
    attacker_types = sorted(pool_df["attacker_type"].dropna().unique())

    # 预设的 per-type 配额模式（假设有 5 种 attacker_type）
    val_pattern = [4, 5, 4, 5, 4]
    test_pattern = [5, 4, 5, 4, 5]

    for i, t in enumerate(attacker_types):
        g = pool_df[pool_df["attacker_type"] == t]
        run_ids = g["run_id"].tolist()
        rng.shuffle(run_ids)

        if i < len(val_pattern):
            n_val_t = min(val_pattern[i], len(run_ids))
            n_test_t = min(test_pattern[i], max(0, len(run_ids) - n_val_t))
        else:
            # 回退策略：如果 attacker_types 数量超出预设，就简单按一半一半切
            n_val_t = len(run_ids) // 2
            n_test_t = len(run_ids) - n_val_t

        val.extend(run_ids[:n_val_t])
        test.extend(run_ids[n_val_t:n_val_t + n_test_t])

    return {"train": train, "val": val, "test": test}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs_csv", type=str, default="data/processed/manifests/runs.csv")
    ap.add_argument("--out_json", type=str, default="data/processed/splits/split_v1.json")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--train_ratio", type=float, default=0.8)
    ap.add_argument("--val_ratio", type=float, default=0.1)
    args = ap.parse_args()

    runs_csv = Path(args.runs_csv)
    out_json = Path(args.out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(runs_csv)

    split = split_runs_per_config(df, seed=args.seed)

    payload = {
        "version": "per_config_4train_1pool_type_stratified",
        "seed": args.seed,
        "counts": {k: len(v) for k, v in split.items()},
        "runs": split,
    }
    out_json.write_text(json.dumps(payload, indent=2))
    print(f"Wrote split file: {out_json}")
    print("Counts:", payload["counts"])


if __name__ == "__main__":
    main()