from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set, Tuple

import pandas as pd
from torch.utils.data import Dataset
from transformers import PreTrainedTokenizerBase

from .gridsybil_plausibility_utils import (
    DEFAULT_FEATURE_INCLUDE_PREFIXES,
    answer_suffix_text_from_label,
    build_plausibility_prompt,
)


@dataclass
class GridSybilPlausibilityDatasetConfig:
    parquet_dir: Path
    max_seq_len: int = 4096
    reserve_answer_tokens: int = 32
    simulate_budget_cutoff: bool = True
    add_eos_token: bool = True
    prompt_variant: str = "default"
    prompt_exclude_columns: Set[str] | None = None
    prompt_include_prefixes: List[str] | None = None
    prompt_include_columns: List[str] | None = None
    feature_name_style: str = "descriptive"
    attack_sample_weight: float = 1.0
    benign_sample_weight: float = 1.0
    max_train_rows: int = 0
    max_val_rows: int = 0
    max_test_rows: int = 0
    subsample_seed: int = 42


class ListBackedDataset(Dataset):
    def __init__(self, rows: list[dict]):
        self.rows = rows

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, idx: int | str) -> Any:
        if isinstance(idx, str):
            return [row[idx] for row in self.rows]
        return self.rows[idx]

    def select(self, indices) -> "ListBackedDataset":
        return ListBackedDataset([self.rows[i] for i in indices])


def _warn_missing_prompt_columns(raw_splits: Dict[str, Dataset], include_columns: List[str]) -> None:
    if not include_columns:
        return
    present: Set[str] = set()
    for ds in raw_splits.values():
        if len(ds) == 0:
            continue
        first = ds[0]
        if isinstance(first, dict):
            present.update(first.keys())
    missing = [c for c in include_columns if c not in present]
    if missing:
        preview = ", ".join(missing[:20])
        more = " ..." if len(missing) > 20 else ""
        print(
            "[dataset] warning: some prompt_include_columns are missing from loaded splits: "
            f"{preview}{more}",
            flush=True,
        )


def _jsonable_row(row: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in row.items():
        if hasattr(v, "item"):
            try:
                out[k] = v.item()
                continue
            except Exception:
                pass
        out[k] = v
    return out


def _sample_df(df: pd.DataFrame, max_rows: int, seed: int) -> pd.DataFrame:
    if max_rows <= 0 or len(df) <= max_rows:
        return df
    return df.sample(n=max_rows, random_state=seed).reset_index(drop=True)


def load_parquet_rows(path: Path, max_rows: int = 0, seed: int = 42) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"Missing parquet file: {path}")
    df = pd.read_parquet(path)
    df = _sample_df(df, max_rows=max_rows, seed=seed)
    rows = [_jsonable_row(r) for r in df.to_dict(orient="records")]
    return rows


def load_raw_gridsybil_plausibility_splits(
    config: GridSybilPlausibilityDatasetConfig,
) -> dict[str, Dataset]:
    base = config.parquet_dir
    raw_train = ListBackedDataset(
        load_parquet_rows(
            base / "train.parquet",
            max_rows=config.max_train_rows,
            seed=config.subsample_seed,
        )
    )
    raw_val = ListBackedDataset(
        load_parquet_rows(
            base / "val.parquet",
            max_rows=config.max_val_rows,
            seed=config.subsample_seed + 1,
        )
    )
    raw_test = ListBackedDataset(
        load_parquet_rows(
            base / "test.parquet",
            max_rows=config.max_test_rows,
            seed=config.subsample_seed + 2,
        )
    )
    return {"train": raw_train, "val": raw_val, "test": raw_test}


def tokenize_gridsybil_plausibility_example(
    example: dict,
    tokenizer: PreTrainedTokenizerBase,
    config: GridSybilPlausibilityDatasetConfig,
) -> dict:
    if "label" not in example:
        raise ValueError(f"Missing label in sample. keys={list(example.keys())[:20]}")
    label = int(example["label"])
    if label not in (0, 1):
        raise ValueError(f"Invalid binary label={label}; expected 0 or 1.")

    exclude_columns = set(config.prompt_exclude_columns or set())
    prompt_build = build_plausibility_prompt(
        sample=example,
        tokenizer=tokenizer,
        simulate_budget_cutoff=config.simulate_budget_cutoff,
        total_budget=config.max_seq_len,
        reserve_answer_tokens=config.reserve_answer_tokens,
        prompt_variant=config.prompt_variant,
        feature_name_style=config.feature_name_style,
        include_prefixes=config.prompt_include_prefixes,
        include_columns=list(config.prompt_include_columns or []),
        exclude_columns=exclude_columns,
    )
    prompt_ids = tokenizer(
        prompt_build.prompt_text,
        truncation=False,
        add_special_tokens=False,
    )["input_ids"]
    target_ids = tokenizer(
        answer_suffix_text_from_label(label),
        truncation=False,
        add_special_tokens=False,
    )["input_ids"]
    if config.add_eos_token and tokenizer.eos_token_id is not None:
        target_ids = list(target_ids) + [tokenizer.eos_token_id]
    else:
        target_ids = list(target_ids)

    input_ids = list(prompt_ids) + target_ids
    if len(input_ids) > config.max_seq_len:
        raise ValueError(
            "Plausibility sample exceeds max_seq_len after prompt truncation. "
            f"message_id={example.get('message_id', 'NA')} total_tokens={len(input_ids)} "
            f"max_seq_len={config.max_seq_len} reserve_answer_tokens={config.reserve_answer_tokens}"
        )
    labels = ([-100] * len(prompt_ids)) + target_ids
    attention_mask = [1] * len(input_ids)

    pad_id = tokenizer.pad_token_id
    if pad_id is None:
        raise ValueError("Tokenizer must define pad_token_id.")
    pad_len = config.max_seq_len - len(input_ids)
    if pad_len > 0:
        input_ids = input_ids + ([pad_id] * pad_len)
        labels = labels + ([-100] * pad_len)
        attention_mask = attention_mask + ([0] * pad_len)

    return {
        "input_ids": input_ids,
        "attention_mask": attention_mask,
        "labels": labels,
        "length": int(sum(attention_mask)),
        "prompt_length": len(prompt_ids),
        "target_length": len(target_ids),
        "label": label,
        "label_weight": float(
            config.attack_sample_weight if label == 1 else config.benign_sample_weight
        ),
        "is_truncated_features": int(prompt_build.is_truncated_features),
        "features_total": int(prompt_build.features_total),
        "features_kept": int(prompt_build.features_kept),
    }


def load_gridsybil_plausibility_datasets(
    parquet_dir: str | Path,
    tokenizer: PreTrainedTokenizerBase,
    max_seq_len: int = 4096,
    reserve_answer_tokens: int = 32,
    simulate_budget_cutoff: bool = True,
    add_eos_token: bool = True,
    prompt_variant: str = "default",
    prompt_exclude_columns: Iterable[str] | None = None,
    prompt_include_prefixes: Iterable[str] | None = None,
    prompt_include_columns: Iterable[str] | None = None,
    feature_name_style: str = "descriptive",
    attack_sample_weight: float = 1.0,
    benign_sample_weight: float = 1.0,
    max_train_rows: int = 0,
    max_val_rows: int = 0,
    max_test_rows: int = 0,
    subsample_seed: int = 42,
) -> Tuple[Dataset, Dataset, Dataset]:
    config = GridSybilPlausibilityDatasetConfig(
        parquet_dir=Path(parquet_dir),
        max_seq_len=max_seq_len,
        reserve_answer_tokens=reserve_answer_tokens,
        simulate_budget_cutoff=simulate_budget_cutoff,
        add_eos_token=add_eos_token,
        prompt_variant=prompt_variant,
        prompt_exclude_columns=set(prompt_exclude_columns or []),
        prompt_include_prefixes=(
            list(DEFAULT_FEATURE_INCLUDE_PREFIXES)
            if prompt_include_prefixes is None
            else list(prompt_include_prefixes)
        ),
        prompt_include_columns=list(prompt_include_columns or []),
        feature_name_style=feature_name_style,
        attack_sample_weight=float(attack_sample_weight),
        benign_sample_weight=float(benign_sample_weight),
        max_train_rows=max_train_rows,
        max_val_rows=max_val_rows,
        max_test_rows=max_test_rows,
        subsample_seed=subsample_seed,
    )
    raw_splits = load_raw_gridsybil_plausibility_splits(config)
    _warn_missing_prompt_columns(raw_splits, list(config.prompt_include_columns or []))

    tokenized: dict[str, Dataset] = {}
    for split_name, raw_ds in raw_splits.items():
        tokenized_rows = [
            tokenize_gridsybil_plausibility_example(
                example,
                tokenizer=tokenizer,
                config=config,
            )
            for example in raw_ds
        ]
        tokenized[split_name] = ListBackedDataset(tokenized_rows)

    return tokenized["train"], tokenized["val"], tokenized["test"]
