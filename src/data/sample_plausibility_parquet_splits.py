from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Dict, Tuple

import pandas as pd


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Sample fixed-size train/val/test parquet subsets for HPC runs."
    )
    ap.add_argument("--in_dir", type=str, required=True, help="Input parquet split directory.")
    ap.add_argument("--out_dir", type=str, required=True, help="Output sampled parquet directory.")
    ap.add_argument("--train_rows", type=int, default=50000)
    ap.add_argument("--val_rows", type=int, default=2000)
    ap.add_argument("--test_rows", type=int, default=50000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument(
        "--stratify_label",
        action="store_true",
        help="Stratified sampling by label column if available.",
    )
    ap.add_argument("--label_col", type=str, default="label")
    return ap.parse_args()


def _sample_df(
    df: pd.DataFrame,
    n: int,
    seed: int,
    stratify_label: bool,
    label_col: str,
) -> pd.DataFrame:
    if n <= 0 or len(df) <= n:
        return df.reset_index(drop=True)

    if stratify_label and label_col in df.columns:
        parts = []
        total = len(df)
        # Proportional allocation by label
        grouped = df.groupby(label_col, sort=False)
        alloc = {}
        for k, g in grouped:
            alloc[k] = int(round(n * (len(g) / total)))
        # Fix rounding drift
        drift = n - sum(alloc.values())
        keys = list(alloc.keys())
        i = 0
        while drift != 0 and keys:
            k = keys[i % len(keys)]
            alloc[k] += 1 if drift > 0 else -1 if alloc[k] > 1 else 0
            drift = n - sum(alloc.values())
            i += 1
            if i > 10000:
                break

        for k, g in grouped:
            nk = max(0, min(len(g), alloc.get(k, 0)))
            if nk > 0:
                parts.append(g.sample(n=nk, random_state=seed))
        out = pd.concat(parts, axis=0)
        if len(out) > n:
            out = out.sample(n=n, random_state=seed)
        elif len(out) < n:
            remain = df.drop(index=out.index)
            add_n = min(len(remain), n - len(out))
            if add_n > 0:
                out = pd.concat([out, remain.sample(n=add_n, random_state=seed)], axis=0)
        return out.sample(frac=1.0, random_state=seed).reset_index(drop=True)

    return df.sample(n=n, random_state=seed).reset_index(drop=True)


def _read_parquet(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Missing split file: {path}")
    return pd.read_parquet(path)


def _write_parquet(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)


def _label_dist(df: pd.DataFrame, label_col: str) -> Dict[str, int]:
    if label_col not in df.columns:
        return {}
    vc = df[label_col].value_counts(dropna=False).to_dict()
    return {str(k): int(v) for k, v in vc.items()}


def sample_one_split(
    in_path: Path,
    out_path: Path,
    n_rows: int,
    seed: int,
    stratify_label: bool,
    label_col: str,
    split_name: str,
) -> Tuple[int, int, Dict[str, int], Dict[str, int]]:
    t0 = time.time()
    print(f"[{split_name}] start input={in_path} output={out_path}", flush=True)

    t_read = time.time()
    df = _read_parquet(in_path)
    print(
        f"[{split_name}] read parquet done rows={len(df)} elapsed={time.time() - t_read:.1f}s",
        flush=True,
    )
    before_n = len(df)
    before_dist = _label_dist(df, label_col)

    t_sample = time.time()
    sampled = _sample_df(
        df=df,
        n=n_rows,
        seed=seed,
        stratify_label=stratify_label,
        label_col=label_col,
    )
    print(
        f"[{split_name}] sampling done sampled_rows={len(sampled)} "
        f"elapsed={time.time() - t_sample:.1f}s",
        flush=True,
    )
    after_n = len(sampled)
    after_dist = _label_dist(sampled, label_col)

    t_write = time.time()
    _write_parquet(sampled, out_path)
    print(
        f"[{split_name}] write parquet done elapsed={time.time() - t_write:.1f}s",
        flush=True,
    )
    print(f"[{split_name}] split total elapsed={time.time() - t0:.1f}s", flush=True)
    return before_n, after_n, before_dist, after_dist


def main() -> None:
    args = parse_args()
    t_all = time.time()

    in_dir = Path(args.in_dir)
    out_dir = Path(args.out_dir)

    spec = {
        "train": args.train_rows,
        "val": args.val_rows,
        "test": args.test_rows,
    }

    meta = {
        "source_dir": str(in_dir),
        "output_dir": str(out_dir),
        "seed": int(args.seed),
        "stratify_label": bool(args.stratify_label),
        "label_col": args.label_col,
        "splits": {},
    }

    total_splits = len(spec)
    for i, (split, n_rows) in enumerate(spec.items(), start=1):
        split_t0 = time.time()
        in_path = in_dir / f"{split}.parquet"
        out_path = out_dir / f"{split}.parquet"
        before_n, after_n, before_dist, after_dist = sample_one_split(
            in_path=in_path,
            out_path=out_path,
            n_rows=n_rows,
            seed=args.seed + i - 1,
            stratify_label=args.stratify_label,
            label_col=args.label_col,
            split_name=split,
        )
        meta["splits"][split] = {
            "input_rows": before_n,
            "sampled_rows": after_n,
            "requested_rows": int(n_rows),
            "input_label_dist": before_dist,
            "sampled_label_dist": after_dist,
        }
        print(
            f"[{split}] input={before_n} sampled={after_n} requested={n_rows}",
            flush=True,
        )
        if after_dist:
            print(f"[{split}] label_dist={after_dist}", flush=True)
        elapsed_all = time.time() - t_all
        done = i
        avg_per_split = elapsed_all / max(1, done)
        eta = avg_per_split * max(0, total_splits - done)
        print(
            f"[progress] splits={done}/{total_splits} "
            f"last_split_elapsed={time.time() - split_t0:.1f}s "
            f"total_elapsed={elapsed_all:.1f}s eta={eta:.1f}s",
            flush=True,
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    meta_path = out_dir / "sampling_meta.json"
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[done] all splits elapsed={time.time() - t_all:.1f}s", flush=True)
    print(f"meta written: {meta_path}", flush=True)


if __name__ == "__main__":
    main()