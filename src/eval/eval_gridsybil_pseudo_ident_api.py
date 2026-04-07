"""API-based evaluation for GridSybil pseudo-entity identification.

This script evaluates (zero-shot / few-shot) via an OpenAI-compatible endpoint
using the same prompt + truncation logic as local evaluation.

Key points:
- Test episodes are selected by `test_id_file` (episode ids, one per line).
- Few-shot demonstrations are selected by `few_shot_id_file` (train ids, one per line,
  or a YAML file containing ids).
- K demos are used by taking the first K entries from `few_shot_id_file`.
- Prompt construction uses `build_pseudo_ident_prompt()` from training utils.
- For few-shot, demo "total_budget" is reduced to avoid context explosion.

Output:
- Prints `METRICS_JSON: {...}` to stdout.
- Writes:
  - overall: outputs/eval_integration_localtest/overall_{run_id}_k{K}.json
  - bucket csv: outputs/eval_integration_localtest/bucket_{run_id}_k{K}.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import time
import gzip
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Literal, Optional, Set, Tuple
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import urljoin

import yaml
from transformers import AutoTokenizer

from ..training.dataset_gridsybil_pseudo_ident import load_jsonl_rows
from ..training.dataset_sanity_gridsybil_pseudo_ident import extract_json_array
from ..training.gridsybil_pseudo_ident_utils import (
    answer_suffix_text_from_ids,
    build_pseudo_ident_prompt,
)


def _norm_id(x: Any) -> str:
    if x is None:
        return ""
    return str(x).strip()


def read_text_lines(path: Path) -> Iterable[str]:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                yield line
    else:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                yield line


def load_id_list(path: Path) -> List[str]:
    if not path.exists():
        raise FileNotFoundError(f"Missing id list file: {path}")

    if path.suffix in {".yaml", ".yml"}:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(cfg, dict):
            # common keys
            for k in ["ids", "episode_ids", "train_episode_ids", "few_shot_episode_ids"]:
                if k in cfg and isinstance(cfg[k], list):
                    return [str(x).strip() for x in cfg[k] if str(x).strip()]
            raise ValueError(f"Unsupported yaml schema in {path}")
        if isinstance(cfg, list):
            return [str(x).strip() for x in cfg if str(x).strip()]
        raise ValueError(f"Unsupported yaml content in {path}")

    # txt/jsonl: one id per line
    out: List[str] = []
    for line in read_text_lines(path):
        s = line.strip()
        if s:
            out.append(s)
    return out


def candidate_bin(n: int) -> str:
    if n <= 8:
        return "less than 9"
    if n <= 16:
        return "9 to 16"
    if n <= 24:
        return "17 to 24"
    if n <= 32:
        return "25 to 32"
    return "more than 32"


def visible_attacker_bin(n: int) -> str:
    if n <= 1:
        return "0 to 1"
    if n == 4:
        return "2 to 4"
    if n <= 8:
        return "5 to 8"
    if n <= 14:
        return "9 to 14"
    return "more than 14"


def micro_from_counts(tp: int, fp: int, fn: int, tn: int) -> Dict[str, float]:
    def safe_div(num: float, den: float) -> float:
        return num / den if den else 0.0

    precision = safe_div(tp, tp + fp)
    recall = safe_div(tp, tp + fn)
    f1 = safe_div(2 * precision * recall, precision + recall)
    accuracy = safe_div(tp + tn, tp + fp + fn + tn)
    return {
        "micro_accuracy": accuracy,
        "micro_precision": precision,
        "micro_recall": recall,
        "micro_f1": f1,
    }


def micro_counts_for_episode(
    candidates: List[str],
    gold_pos: Set[str],
    pred_visible: Set[str],
) -> Tuple[int, int, int, int]:
    """TP/FP/FN/TN over visible candidates (attacker = positive)."""
    tp = fp = fn = tn = 0
    vis = list(dict.fromkeys(candidates))
    for c in vis:
        t = 1 if c in gold_pos else 0
        p = 1 if c in pred_visible else 0
        if t == 1 and p == 1:
            tp += 1
        elif t == 0 and p == 1:
            fp += 1
        elif t == 1 and p == 0:
            fn += 1
        else:
            tn += 1
    return tp, fp, fn, tn


def request_openai_chat_completion(
    *,
    api_key: str,
    model: str,
    prompt: str,
    temperature: float,
    max_tokens: int,
    base_url: str,
    max_retries: int = 6,
    retry_backoff_s: float = 1.5,
) -> str:
    """
    OpenAI-compatible chat completion via built-in urllib to avoid extra deps.
    """
    url = urljoin(base_url.rstrip("/") + "/", "v1/chat/completions")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_completion_tokens": max_tokens,
    }

    data = json.dumps(payload).encode("utf-8")

    for attempt in range(max_retries):
        req = Request(url, data=data, headers=headers, method="POST")
        try:
            with urlopen(req, timeout=1200) as resp:
                raw = resp.read().decode("utf-8")
                js = json.loads(raw)
            return js["choices"][0]["message"]["content"]
        except HTTPError as e:
            # Retry on rate limit / transient errors.
            status = getattr(e, "code", None)
            if status in (429, 500, 502, 503, 504) and attempt < max_retries - 1:
                sleep_s = retry_backoff_s * (2**attempt) + random.random() * 0.5
                time.sleep(sleep_s)
                continue
            raise
    raise RuntimeError("Failed after retries (unreachable).")


@dataclass
class EvalConfig:
    tokenizer_model_name: str
    jsonl_dir: Path
    split: str
    max_seq_len: int
    reserve_answer_tokens: int
    entity_sort_policy: str
    simulate_budget_cutoff: bool
    add_eos_token: bool
    prompt_variant: str

    openai_model: str
    max_completion_tokens: int
    temperature: float

    test_id_file: Path
    few_shot_id_file: Path
    shot_mode: str
    max_samples: int
    log_raw_responses: bool
    run_id: str

    openai_base_url: str


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="API eval (zero/few-shot) for GridSybil pseudo-ident.")
    p.add_argument("--config", type=str, required=True, help="Path to gridsybil_api_eval.yaml")
    p.add_argument("--k", type=int, default=None, help="Override number of few-shot demos (K).")
    p.add_argument("--output_dir", type=str, default="outputs/eval_integration_localtest")
    p.add_argument("--base_url", type=str, default=None, help="Override OpenAI base_url (default uses OpenAI public).")
    return p.parse_args()


def load_yaml_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise ValueError(f"Config {path} must be a mapping.")
    return cfg


def main() -> None:
    args = parse_args()
    cfg_y = load_yaml_config(Path(args.config))

    jsonl_dir = Path(cfg_y.get("jsonl_dir", "data/processed/jsonl/gridsybil_pseudo_ident_balanced_a"))
    if not jsonl_dir.is_absolute():
        jsonl_dir = Path.cwd() / jsonl_dir

    test_id_file = Path(cfg_y["test_id_file"])
    if not test_id_file.is_absolute():
        test_id_file = Path.cwd() / test_id_file

    few_shot_id_file_raw = cfg_y.get("few_shot_id_file")
    if few_shot_id_file_raw is None:
        raise ValueError("Missing few_shot_id_file in config.")
    few_shot_id_file = Path(few_shot_id_file_raw)
    if not few_shot_id_file.is_absolute():
        few_shot_id_file = Path.cwd() / few_shot_id_file
    # Fallback: config might point to yaml but we actually have txt.
    if not few_shot_id_file.exists() and few_shot_id_file.suffix in {".yaml", ".yml"}:
        alt = few_shot_id_file.with_suffix(".txt")
        if alt.exists():
            few_shot_id_file = alt

    max_samples = int(cfg_y.get("max_samples", 0))
    if max_samples < 0:
        raise ValueError("max_samples must be >= 0.")

    shot_mode = str(cfg_y.get("shot_mode", "zero")).strip().lower()
    cfg_k: Optional[int] = args.k
    if cfg_k is None:
        if shot_mode == "zero":
            cfg_k = 0
        else:
            # default to K=8 if few-shot file is k8, but still allow user override by --k
            cfg_k = int(cfg_y.get("k", 8))
    k = int(cfg_k)

    base_url = args.base_url or str(cfg_y.get("openai_base_url", "https://api.openai.com"))

    ecfg = EvalConfig(
        tokenizer_model_name=str(cfg_y.get("tokenizer_model_name")),
        jsonl_dir=jsonl_dir,
        split=str(cfg_y.get("split", "test")),
        max_seq_len=int(cfg_y.get("max_seq_len", 4160)),
        reserve_answer_tokens=int(cfg_y.get("reserve_answer_tokens", 320)),
        entity_sort_policy=str(cfg_y.get("entity_sort_policy", "msgs_lifetime_distance")),
        simulate_budget_cutoff=bool(cfg_y.get("simulate_budget_cutoff", True)),
        add_eos_token=bool(cfg_y.get("add_eos_token", True)),
        prompt_variant=str(cfg_y.get("prompt_variant", "default")),
        openai_model=str(cfg_y.get("openai_model")),
        max_completion_tokens=int(cfg_y.get("max_completion_tokens", 128)),
        temperature=float(cfg_y.get("temperature", 0.0)),
        test_id_file=test_id_file,
        few_shot_id_file=few_shot_id_file,
        shot_mode=shot_mode,
        max_samples=max_samples,
        log_raw_responses=bool(cfg_y.get("log_raw_responses", False)),
        run_id=str(cfg_y.get("run_id", "api_eval")),
        openai_base_url=base_url,
    )

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise EnvironmentError("Missing OPENAI_API_KEY environment variable.")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load id lists
    test_ids = load_id_list(ecfg.test_id_file)
    if ecfg.max_samples and ecfg.max_samples > 0:
        test_ids = test_ids[: ecfg.max_samples]
    # k demos
    few_ids = load_id_list(ecfg.few_shot_id_file) if k > 0 else []
    demos_ids = few_ids[:k] if k > 0 else []

    print(
        json.dumps(
            {
                "run_id": ecfg.run_id,
                "k": k,
                "test_split": ecfg.split,
                "n_test_ids": len(test_ids),
                "n_demo_ids": len(demos_ids),
                "test_id_file": str(ecfg.test_id_file),
                "few_shot_id_file": str(ecfg.few_shot_id_file),
                "prompt_variant": ecfg.prompt_variant,
                "openai_model": ecfg.openai_model,
                "base_url": ecfg.openai_base_url,
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )

    tokenizer = AutoTokenizer.from_pretrained(ecfg.tokenizer_model_name)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"

    # Load JSONL rows by ids (stream to dict)
    def load_rows_by_ids(jsonl_path: Path, ids: List[str]) -> Dict[str, Dict[str, Any]]:
        id_set = set(ids)
        found: Dict[str, Dict[str, Any]] = {}
        with jsonl_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                ex = json.loads(line)
                ep_id = str(ex.get("id"))
                if ep_id in id_set:
                    found[ep_id] = ex
                    if len(found) >= len(id_set):
                        break
        missing = [x for x in ids if x not in found]
        if missing:
            raise ValueError(f"Missing {len(missing)} ids from {jsonl_path} (e.g. {missing[:5]}).")
        return found

    train_jsonl = ecfg.jsonl_dir / "train.jsonl"
    test_jsonl = ecfg.jsonl_dir / f"{ecfg.split}.jsonl"

    demos_by_id: Dict[str, Dict[str, Any]] = {}
    if k > 0:
        demos_by_id = load_rows_by_ids(train_jsonl, demos_ids)

    test_by_id = load_rows_by_ids(test_jsonl, test_ids)

    # Bucket aggregators
    @dataclass
    class BucketAgg:
        rows: int = 0
        episodes: int = 0
        tp: int = 0
        fp: int = 0
        fn: int = 0
        tn: int = 0
        parse_ok_pseudo_sum: int = 0
        oob_pseudo_sum: int = 0
        episode_exact_sum: int = 0
        episode_parse_sum: int = 0
        episode_oob_sum: int = 0

        episode_seen: bool = False

        def update(
            self,
            *,
            episode_id: str,
            visible_candidates_len: int,
            tp: int,
            fp: int,
            fn: int,
            tn: int,
            parse_ok_ep: int,
            has_oob_pred_ep: int,
            is_exact_ep: int,
        ) -> None:
            self.rows += int(visible_candidates_len)
            self.tp += int(tp)
            self.fp += int(fp)
            self.fn += int(fn)
            self.tn += int(tn)
            self.parse_ok_pseudo_sum += int(parse_ok_ep) * int(visible_candidates_len)
            self.oob_pseudo_sum += int(has_oob_pred_ep) * int(visible_candidates_len)
            # Each episode contributes once per bucket.
            if not self.episode_seen:
                # We rely on caller passing updates once per episode per bucket.
                self.episodes += 1
                self.episode_exact_sum += int(is_exact_ep)
                self.episode_parse_sum += int(parse_ok_ep)
                self.episode_oob_sum += int(has_oob_pred_ep)
                self.episode_seen = True

    # But we need separate "seen per episode" per bucket value. We'll instead avoid this complexity
    # by accumulating directly in dicts keyed by bucket_name/bucket_value, and using "episode set".
    @dataclass
    class BucketAgg2:
        rows: int = 0
        episodes: int = 0
        tp: int = 0
        fp: int = 0
        fn: int = 0
        tn: int = 0
        parse_ok_pseudo_sum: int = 0
        oob_pseudo_sum: int = 0
        episode_exact_sum: int = 0
        episode_parse_sum: int = 0
        episode_oob_sum: int = 0
        episode_ids: Set[str] = None  # type: ignore[assignment]

        def __post_init__(self) -> None:
            self.episode_ids = set()

        def update(
            self,
            *,
            episode_id: str,
            visible_candidates_len: int,
            tp: int,
            fp: int,
            fn: int,
            tn: int,
            parse_ok_ep: int,
            has_oob_pred_ep: int,
            is_exact_ep: int,
        ) -> None:
            self.rows += int(visible_candidates_len)
            self.tp += int(tp)
            self.fp += int(fp)
            self.fn += int(fn)
            self.tn += int(tn)
            self.parse_ok_pseudo_sum += int(parse_ok_ep) * int(visible_candidates_len)
            self.oob_pseudo_sum += int(has_oob_pred_ep) * int(visible_candidates_len)
            if episode_id not in self.episode_ids:
                self.episode_ids.add(episode_id)
                self.episodes += 1
                self.episode_exact_sum += int(is_exact_ep)
                self.episode_parse_sum += int(parse_ok_ep)
                self.episode_oob_sum += int(has_oob_pred_ep)

        def to_row(self, bucket_name: str, bucket_value: str) -> Dict[str, Any]:
            def safe_div(num: float, den: float) -> float:
                return num / den if den else 0.0

            micro = micro_from_counts(self.tp, self.fp, self.fn, self.tn)
            return {
                "bucket_name": bucket_name,
                "bucket_value": bucket_value,
                "rows": self.rows,
                "episodes": self.episodes,
                "tp": self.tp,
                "fp": self.fp,
                "fn": self.fn,
                "tn": self.tn,
                "parse_ok_rate": safe_div(self.parse_ok_pseudo_sum, self.rows),
                "oob_row_rate": safe_div(self.oob_pseudo_sum, self.rows),
                "episode_exact_match_rate": safe_div(self.episode_exact_sum, self.episodes),
                "episode_parse_ok_rate": safe_div(self.episode_parse_sum, self.episodes),
                "episode_oob_rate": safe_div(self.episode_oob_sum, self.episodes),
                **micro,
            }

    buckets: Dict[Tuple[str, str], BucketAgg2] = {}

    # Overall accumulators (pseudo-level)
    total_tp = total_fp = total_fn = total_tn = 0
    exact_match = 0
    parse_ok_n = 0
    prompt_too_long = 0
    pred_oob_episodes = 0
    pseudo_rows_total = 0
    parse_ok_pseudo_sum = 0
    oob_pseudo_sum = 0

    # Prompt building budgets for demos
    if k > 0:
        demo_total_budget = max(256, ecfg.max_seq_len // (k + 1))
        demo_reserve_answer = max(64, ecfg.reserve_answer_tokens // (k + 1))
    else:
        demo_total_budget = 0
        demo_reserve_answer = 0

    # Pre-build demo prefix (depends on k and budgets)
    demo_prefix_texts: List[str] = []
    if k > 0:
        for demo_id in demos_ids:
            ex = demos_by_id[demo_id]
            pb = build_pseudo_ident_prompt(
                sample=ex,
                tokenizer=tokenizer,
                simulate_budget_cutoff=ecfg.simulate_budget_cutoff,
                total_budget=demo_total_budget,
                reserve_answer_tokens=demo_reserve_answer,
                entity_sort_policy=ecfg.entity_sort_policy,
                prompt_variant=ecfg.prompt_variant,
            )
            demo_text = pb.prompt_text + answer_suffix_text_from_ids(pb.visible_output_ids)
            demo_prefix_texts.append(demo_text)

    demos_prefix = "\n\n".join(demo_prefix_texts).strip()

    # Optional raw response logging
    raw_log_fh: Optional[Any] = None
    raw_log_path: Optional[Path] = None
    if ecfg.log_raw_responses:
        raw_log_path = output_dir / f"raw_{ecfg.run_id}_k{k}.jsonl"
        raw_log_path.parent.mkdir(parents=True, exist_ok=True)
        raw_log_fh = raw_log_path.open("w", encoding="utf-8")

    test_rows: List[Dict[str, Any]] = [test_by_id[tid] for tid in test_ids]

    t0 = time.time()
    for i, ex in enumerate(test_rows):
        ep_id = str(ex.get("id"))
        pb_test = build_pseudo_ident_prompt(
            sample=ex,
            tokenizer=tokenizer,
            simulate_budget_cutoff=ecfg.simulate_budget_cutoff,
            total_budget=ecfg.max_seq_len,
            reserve_answer_tokens=ecfg.reserve_answer_tokens,
            entity_sort_policy=ecfg.entity_sort_policy,
            prompt_variant=ecfg.prompt_variant,
        )

        prompt_text = pb_test.prompt_text
        if k > 0:
            prompt_text = demos_prefix + "\n\n" + prompt_text

        # Safety: skip extremely long prompts
        prompt_len = len(tokenizer(prompt_text, truncation=False)["input_ids"])
        if prompt_len > ecfg.max_seq_len * 2:
            prompt_too_long += 1
            continue

        # Visible sets
        visible_candidates = list(pb_test.visible_candidate_ids)
        gold_visible = [_norm_id(x) for x in pb_test.visible_output_ids if _norm_id(x)]
        gold_set = set(gold_visible)
        visible_set = set(visible_candidates)

        # Call API
        gen_text = request_openai_chat_completion(
            api_key=api_key,
            model=ecfg.openai_model,
            prompt=prompt_text,
            temperature=ecfg.temperature,
            max_tokens=ecfg.max_completion_tokens,
            base_url=ecfg.openai_base_url,
        )

        # Parse predicted ids
        try:
            pred_list = extract_json_array(gen_text)
            pred_list = [_norm_id(x) for x in pred_list]
            pred_list = [x for x in pred_list if x]
            pred_seen: Set[str] = set()
            pred_unique: List[str] = []
            for x in pred_list:
                if x not in pred_seen:
                    pred_seen.add(x)
                    pred_unique.append(x)
            pred_list = pred_unique
            p_ok = True
        except Exception:
            pred_list = []
            p_ok = False

        if p_ok:
            parse_ok_n += 1

        pred_visible = {x for x in pred_list if x in visible_set}
        has_oob_pred_ep = int(any(x not in visible_set for x in pred_list))
        if has_oob_pred_ep:
            pred_oob_episodes += 1

        tp, fp, fn, tn = micro_counts_for_episode(visible_candidates, gold_set, pred_visible)
        is_exact_ep = int(gold_set == pred_visible)
        exact_match += is_exact_ep

        total_tp += tp
        total_fp += fp
        total_fn += fn
        total_tn += tn

        visible_candidates_len = len(list(dict.fromkeys(visible_candidates)))
        pseudo_rows_total += visible_candidates_len
        parse_ok_pseudo_sum += int(p_ok) * visible_candidates_len
        oob_pseudo_sum += has_oob_pred_ep * visible_candidates_len

        if raw_log_fh is not None:
            raw_log_fh.write(
                json.dumps(
                    {
                        "episode_id": ep_id,
                        "k": k,
                        "parse_ok": p_ok,
                        "prompt_len": prompt_len,
                        "gen_text": gen_text,
                        "pred_list": pred_list,
                        "gold_visible": gold_visible,
                        "visible_candidates": visible_candidates,
                        "has_oob_pred": has_oob_pred_ep,
                        "is_exact_match": is_exact_ep,
                        "tp": tp,
                        "fp": fp,
                        "fn": fn,
                        "tn": tn,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

        # Buckets
        inp = ex.get("input", {})
        meta = inp.get("meta", {}) if isinstance(inp, dict) else {}
        traffic_regime = meta.get("traffic_regime", None) if isinstance(meta, dict) else None

        n_visible_candidates = len(list(dict.fromkeys(visible_candidates)))
        n_visible_attackers = len(gold_set)
        cand_key = candidate_bin(n_visible_candidates)
        vis_att_key = visible_attacker_bin(n_visible_attackers)
        traffic_key = str(traffic_regime)

        # traffic_regime
        for bucket_name, bucket_value in [
            ("traffic_regime", traffic_key),
            ("n_visible_candidates_bin", cand_key),
            ("n_visible_attackers_bin", vis_att_key),
        ]:
            bkey = (bucket_name, bucket_value)
            if bkey not in buckets:
                buckets[bkey] = BucketAgg2()
            buckets[bkey].update(
                episode_id=ep_id,
                visible_candidates_len=visible_candidates_len,
                tp=tp,
                fp=fp,
                fn=fn,
                tn=tn,
                parse_ok_ep=int(p_ok),
                has_oob_pred_ep=has_oob_pred_ep,
                is_exact_ep=is_exact_ep,
            )

        if (i + 1) % 20 == 0 or (i + 1) == len(test_rows):
            elapsed = time.time() - t0
            speed = (i + 1) / elapsed if elapsed > 0 else 0.0
            print(f"  progress {i+1}/{len(test_rows)} ({speed:.2f} eps/s)", flush=True)

    if raw_log_fh is not None:
        raw_log_fh.close()

    metrics = micro_from_counts(total_tp, total_fp, total_fn, total_tn)
    wall_s = time.time() - t0

    overall = {
        "run_id": ecfg.run_id,
        "model_tag": "api",
        "split": ecfg.split,
        "k": k,
        "n_test_ids": len(test_ids),
        "prompt_too_long": prompt_too_long,
        "parse_ok_rate": (parse_ok_pseudo_sum / pseudo_rows_total) if pseudo_rows_total else 0.0,
        "oob_episode_rate": (pred_oob_episodes / max(1, len(test_rows))) if test_rows else 0.0,
        "micro_accuracy": metrics["micro_accuracy"],
        "micro_precision": metrics["micro_precision"],
        "micro_recall": metrics["micro_recall"],
        "micro_f1": metrics["micro_f1"],
        "episode_exact_match_rate": exact_match / max(1, len(test_rows)),
        "parse_ok": parse_ok_n,
        "pred_oob_episodes": pred_oob_episodes,
        "tp": total_tp,
        "fp": total_fp,
        "fn": total_fn,
        "tn": total_tn,
        "wall_s": wall_s,
        "pseudo_rows_total": pseudo_rows_total,
    }

    print("METRICS_JSON:", json.dumps(overall, ensure_ascii=False), flush=True)

    # Write bucket CSV
    bucket_rows: List[Dict[str, Any]] = []
    for (bucket_name, bucket_value), agg in sorted(buckets.items(), key=lambda x: (x[0][0], x[0][1])):
        bucket_rows.append(agg.to_row(bucket_name, bucket_value))

    overall_path = output_dir / f"overall_{ecfg.run_id}_k{k}.json"
    bucket_path = output_dir / f"bucket_{ecfg.run_id}_k{k}.csv"

    overall_path.write_text(json.dumps(overall, ensure_ascii=False, indent=2), encoding="utf-8")
    with bucket_path.open("w", encoding="utf-8", newline="") as f:
        if bucket_rows:
            keys = list(bucket_rows[0].keys())
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(bucket_rows)

    print(f"Saved overall to {overall_path}", flush=True)
    print(f"Saved bucket to {bucket_path}", flush=True)


if __name__ == "__main__":
    main()

