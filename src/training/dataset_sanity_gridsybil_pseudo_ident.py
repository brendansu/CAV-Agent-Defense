from __future__ import annotations

import argparse
import json
import random
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml
from transformers import AutoTokenizer

from .dataset_gridsybil_pseudo_ident import (
    GridSybilPseudoIdentDatasetConfig,
    tokenize_gridsybil_pseudo_ident_example,
)
from .gridsybil_pseudo_ident_utils import build_pseudo_ident_prompt


def read_jsonl_rows(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
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


def extract_json_array(text: str) -> List[Any]:
    # Robust extraction: find the first [...] block.
    m = re.search(r"\[[^\[\]]*\]", text)
    if not m:
        raise ValueError(f"Cannot find JSON array in decoded text: {text!r}")
    return json.loads(m.group(0))


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Sanity checks for GridSybil pseudo-ident dataset: "
        "input visibility + label consistency under entity truncation."
    )
    ap.add_argument(
        "--config",
        type=str,
        default="configs/qwen2.5_1.5b_gridsybil_pseudo_ident.yaml",
        help="YAML config path used for tokenizer + prompt settings.",
    )
    ap.add_argument(
        "--split",
        type=str,
        default="all",
        choices=["train", "val", "test", "all"],
        help="Which split to check.",
    )
    ap.add_argument(
        "--max_samples_per_split",
        type=int,
        default=128,
        help="Max number of raw samples to tokenize per split (0 means all).",
    )
    ap.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for sampling.",
    )
    ap.add_argument(
        "--assert_decode_samples_per_split",
        type=int,
        default=3,
        help="How many samples per split to decode and verify label==visible attackers.",
    )
    ap.add_argument(
        "--print_prompt_tails",
        action="store_true",
        help="Print prompt tails for decode-verified samples (for manual inspection).",
    )
    ap.add_argument(
        "--max_hidden_attacker_ratio",
        type=float,
        default=None,
        help="Optional: fail if hidden_attacker_ratio exceeds this value.",
    )
    ap.add_argument(
        "--max_truncated_ratio",
        type=float,
        default=None,
        help="Optional: fail if is_truncated_entities ratio exceeds this value.",
    )
    return ap.parse_args()


def summarize_metrics(rows: List[Dict[str, Any]]) -> Dict[str, float]:
    def mean_int(col: str) -> float:
        if not rows:
            return 0.0
        return sum(float(r[col]) for r in rows) / len(rows)

    return {
        "n": float(len(rows)),
        "prompt_len_mean": mean_int("prompt_length"),
        "target_len_mean": mean_int("target_length"),
        "visible_candidates_mean": mean_int("n_visible_candidates"),
        "visible_attackers_mean": mean_int("n_visible_attackers"),
        "hidden_attackers_mean": mean_int("n_attackers_hidden"),
        "truncated_ratio": mean_int("is_truncated_entities"),
        "hidden_attacker_ratio": mean_int("hidden_attacker"),
    }


def main() -> None:
    args = parse_args()
    cfg_path = Path(args.config)
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))

    model_name = cfg["model_name"]
    jsonl_dir = Path(cfg["jsonl_dir"])
    max_seq_len = int(cfg["max_seq_len"])
    reserve_answer_tokens = int(cfg["reserve_answer_tokens"])
    entity_sort_policy = str(cfg["entity_sort_policy"])
    simulate_budget_cutoff = bool(cfg["simulate_budget_cutoff"])
    add_eos_token = bool(cfg["add_eos_token"])

    tokenizer = AutoTokenizer.from_pretrained(model_name)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    dataset_cfg = GridSybilPseudoIdentDatasetConfig(
        jsonl_dir=jsonl_dir,
        max_seq_len=max_seq_len,
        reserve_answer_tokens=reserve_answer_tokens,
        entity_sort_policy=entity_sort_policy,
        simulate_budget_cutoff=simulate_budget_cutoff,
        add_eos_token=add_eos_token,
    )

    split_names = ["train", "val", "test"] if args.split == "all" else [args.split]
    rng = random.Random(args.seed)

    all_metrics: Dict[str, Dict[str, float]] = {}

    print("Sanity config:", flush=True)
    dcfg = asdict(dataset_cfg)
    dcfg["jsonl_dir"] = str(dcfg["jsonl_dir"])
    print(json.dumps(dcfg, ensure_ascii=False), flush=True)

    for split in split_names:
        raw_path = jsonl_dir / f"{split}.jsonl"
        if not raw_path.exists():
            raise FileNotFoundError(f"Missing split file: {raw_path}")
        raw_rows = read_jsonl_rows(raw_path)
        if args.max_samples_per_split > 0:
            k = min(args.max_samples_per_split, len(raw_rows))
            idxs = rng.sample(range(len(raw_rows)), k=k)
            rows_to_tokenize = [raw_rows[i] for i in idxs]
        else:
            rows_to_tokenize = raw_rows

        print(f"\n=== {split} === raw={len(raw_rows)} tokenize={len(rows_to_tokenize)}", flush=True)

        tokenized_rows: List[Dict[str, Any]] = []
        for ex in rows_to_tokenize:
            enc = tokenize_gridsybil_pseudo_ident_example(
                example=ex,
                tokenizer=tokenizer,
                config=dataset_cfg,
            )
            tokenized_rows.append(enc)

        metrics = summarize_metrics(tokenized_rows)
        all_metrics[split] = metrics

        print(
            f"[{split}] prompt_len_mean={metrics['prompt_len_mean']:.1f} "
            f"target_len_mean={metrics['target_len_mean']:.1f} "
            f"visible_candidates_mean={metrics['visible_candidates_mean']:.2f} "
            f"visible_attackers_mean={metrics['visible_attackers_mean']:.2f} "
            f"hidden_attackers_mean={metrics['hidden_attackers_mean']:.2f} "
            f"truncated_ratio={metrics['truncated_ratio']:.3%} "
            f"hidden_attacker_ratio={metrics['hidden_attacker_ratio']:.3%}",
            flush=True,
        )

        if args.max_hidden_attacker_ratio is not None and metrics["hidden_attacker_ratio"] > args.max_hidden_attacker_ratio:
            raise AssertionError(
                f"[{split}] hidden_attacker_ratio={metrics['hidden_attacker_ratio']:.3%} "
                f"> max_hidden_attacker_ratio={args.max_hidden_attacker_ratio:.3%}"
            )
        if args.max_truncated_ratio is not None and metrics["truncated_ratio"] > args.max_truncated_ratio:
            raise AssertionError(
                f"[{split}] truncated_ratio={metrics['truncated_ratio']:.3%} "
                f"> max_truncated_ratio={args.max_truncated_ratio:.3%}"
            )

        # Decode-verify: label ids in tokenized sample must equal visible_output_ids
        if len(rows_to_tokenize) == 0:
            continue
        k_decode = min(args.assert_decode_samples_per_split, len(rows_to_tokenize))
        decode_indices = rng.sample(range(len(rows_to_tokenize)), k=k_decode)

        for local_i in decode_indices:
            ex = rows_to_tokenize[local_i]
            enc = tokenized_rows[local_i]

            prompt_build = build_pseudo_ident_prompt(
                sample=ex,
                tokenizer=tokenizer,
                simulate_budget_cutoff=dataset_cfg.simulate_budget_cutoff,
                total_budget=dataset_cfg.max_seq_len,
                reserve_answer_tokens=dataset_cfg.reserve_answer_tokens,
                entity_sort_policy=dataset_cfg.entity_sort_policy,
            )
            visible_output_ids = list(prompt_build.visible_output_ids)

            # labels: [-100]*prompt + target_ids (+ optional eos)
            label_ids = [tid for tid in enc["labels"] if tid != -100]
            if tokenizer.eos_token_id is not None and label_ids and label_ids[-1] == tokenizer.eos_token_id:
                label_ids = label_ids[:-1]

            decoded_label_text = tokenizer.decode(label_ids, skip_special_tokens=False)
            decoded_label_ids = extract_json_array(decoded_label_text)

            if decoded_label_ids != visible_output_ids:
                print("\n[DECODE MISMATCH]")
                print("id=", ex.get("id"))
                print("visible_output_ids=", visible_output_ids)
                print("decoded_label_text=", decoded_label_text)
                print("decoded_label_ids=", decoded_label_ids)
                print("visible_candidate_ids=", prompt_build.visible_candidate_ids)
                print("full output_ids=", ex.get("output_ids", []))
                if args.print_prompt_tails:
                    print("\n[PROMPT TAIL]")
                    print("\n".join(prompt_build.prompt_text.splitlines()[-25:]))
                raise AssertionError("Decoded label ids do not match visible output ids.")

            if args.print_prompt_tails:
                print("\n[DECODE OK]")
                print("id=", ex.get("id"))
                print("visible_output_ids=", visible_output_ids)
                print("label_text=", decoded_label_text)
                print("[PROMPT TAIL]")
                print("\n".join(prompt_build.prompt_text.splitlines()[-25:]))

    print("\nAll sanity checks passed.", flush=True)
    print("Summary:", flush=True)
    print(json.dumps(all_metrics, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()

