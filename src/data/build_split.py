from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd


def stratified_split(
    df: pd.DataFrame,
    strat_cols: List[str],
    train_ratio: float,
    val_ratio: float,
    seed: int,
) -> Dict[str, List[str]]:
    rng = np.random.default_rng(seed)

    df = df.copy()
    # 只保留健康 run
    df = df[df["status"] == "ok"]
    for c in strat_cols:
        df = df[df[c].notna()]

    # 分层 key
    df["stratum"] = df[strat_cols].astype(str).agg("|".join, axis=1)

    train, val, test = [], [], []
    for _, g in df.groupby("stratum"):
        run_ids = g["run_id"].tolist()
        rng.shuffle(run_ids)

        n = len(run_ids)
        n_train = int(round(n * train_ratio))
        n_val = int(round(n * val_ratio))
        # 保证不超过 n
        n_train = min(n_train, n)
        n_val = min(n_val, n - n_train)
        # 剩下都是 test
        train.extend(run_ids[:n_train])
        val.extend(run_ids[n_train:n_train + n_val])
        test.extend(run_ids[n_train + n_val:])

    # 再全局打乱一下（可选）
    rng.shuffle(train)
    rng.shuffle(val)
    rng.shuffle(test)

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

    split = stratified_split(
        df,
        strat_cols=["attacker_type", "attacker_prob", "start_h"],
        train_ratio=args.train_ratio,
        val_ratio=args.val_ratio,
        seed=args.seed,
    )

    payload = {
        "version": "v1",
        "seed": args.seed,
        "train_ratio": args.train_ratio,
        "val_ratio": args.val_ratio,
        "stratify_by": ["attacker_type", "attacker_prob", "start_h"],
        "counts": {k: len(v) for k, v in split.items()},
        "runs": split,
    }
    out_json.write_text(json.dumps(payload, indent=2))
    print(f"Wrote split file: {out_json}")
    print("Counts:", payload["counts"])


if __name__ == "__main__":
    main()