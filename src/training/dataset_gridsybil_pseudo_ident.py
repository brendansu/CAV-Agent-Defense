from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Tuple

from torch.utils.data import Dataset
from transformers import PreTrainedTokenizerBase

from .gridsybil_pseudo_ident_utils import (
    answer_suffix_text_from_ids,
    build_pseudo_ident_prompt,
)


@dataclass
class GridSybilPseudoIdentDatasetConfig:
    jsonl_dir: Path
    max_seq_len: int = 4096
    reserve_answer_tokens: int = 256
    entity_sort_policy: str = "msgs_lifetime_distance"
    simulate_budget_cutoff: bool = True
    add_eos_token: bool = True


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


def load_jsonl_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON at {path}:{line_no}: {exc}") from exc
    return rows


def load_raw_gridsybil_pseudo_ident_splits(
    config: GridSybilPseudoIdentDatasetConfig,
) -> dict[str, Dataset]:
    base = config.jsonl_dir
    raw_train = ListBackedDataset(load_jsonl_rows(base / "train.jsonl"))
    raw_val = ListBackedDataset(load_jsonl_rows(base / "val.jsonl"))
    raw_test = ListBackedDataset(load_jsonl_rows(base / "test.jsonl"))
    return {"train": raw_train, "val": raw_val, "test": raw_test}


def tokenize_gridsybil_pseudo_ident_example(
    example: dict,
    tokenizer: PreTrainedTokenizerBase,
    config: GridSybilPseudoIdentDatasetConfig,
) -> dict:
    prompt_build = build_pseudo_ident_prompt(
        sample=example,
        tokenizer=tokenizer,
        simulate_budget_cutoff=config.simulate_budget_cutoff,
        total_budget=config.max_seq_len,
        reserve_answer_tokens=config.reserve_answer_tokens,
        entity_sort_policy=config.entity_sort_policy,
    )
    target_ids_visible = list(prompt_build.visible_output_ids)
    prompt_ids = tokenizer(
        prompt_build.prompt_text,
        truncation=False,
        add_special_tokens=False,
    )["input_ids"]
    target_ids = tokenizer(
        answer_suffix_text_from_ids(target_ids_visible),
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
            "GridSybil pseudo-ident sample exceeds max_seq_len after prompt truncation. "
            f"id={example.get('id', 'UNKNOWN_ID')} total_tokens={len(input_ids)} "
            f"max_seq_len={config.max_seq_len} reserve_answer_tokens={config.reserve_answer_tokens} "
            f"visible_attackers={len(target_ids_visible)}"
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
        "n_visible_candidates": len(prompt_build.visible_candidate_ids),
        "n_visible_attackers": len(target_ids_visible),
        "n_attackers_full": len(example.get("output_ids", [])),
        "n_attackers_hidden": len(example.get("output_ids", [])) - len(target_ids_visible),
        "hidden_attacker": int(
            len(target_ids_visible) < len(example.get("output_ids", []))
        ),
        "is_truncated_entities": int(prompt_build.is_truncated_entities),
    }


def load_gridsybil_pseudo_ident_datasets(
    jsonl_dir: str | Path,
    tokenizer: PreTrainedTokenizerBase,
    max_seq_len: int = 4096,
    reserve_answer_tokens: int = 256,
    entity_sort_policy: str = "msgs_lifetime_distance",
    simulate_budget_cutoff: bool = True,
    add_eos_token: bool = True,
) -> Tuple[Dataset, Dataset, Dataset]:
    config = GridSybilPseudoIdentDatasetConfig(
        jsonl_dir=Path(jsonl_dir),
        max_seq_len=max_seq_len,
        reserve_answer_tokens=reserve_answer_tokens,
        entity_sort_policy=entity_sort_policy,
        simulate_budget_cutoff=simulate_budget_cutoff,
        add_eos_token=add_eos_token,
    )
    raw_splits = load_raw_gridsybil_pseudo_ident_splits(config)

    tokenized: dict[str, Dataset] = {}
    for split_name, raw_ds in raw_splits.items():
        tokenized_rows = [
            tokenize_gridsybil_pseudo_ident_example(
                example,
                tokenizer=tokenizer,
                config=config,
            )
            for example in raw_ds
        ]
        tokenized[split_name] = ListBackedDataset(tokenized_rows)

    return tokenized["train"], tokenized["val"], tokenized["test"]
