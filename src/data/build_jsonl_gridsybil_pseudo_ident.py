from __future__ import annotations

import argparse
import json
import random
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterator, List, Set, Tuple


PSEUDO_LOCAL_ID_RE = re.compile(r"^p(\d+)$")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=(
            "Build train/val/test JSONL for GridSybil pseudo-entity identification "
            "from episode JSONL files."
        )
    )
    ap.add_argument(
        "--episodes_dir",
        type=str,
        default="data/processed/episodes_gridsybil_pseudo_identity_v1",
        help="Directory containing per-run episode JSONL files.",
    )
    ap.add_argument(
        "--split_json",
        type=str,
        required=True,
        help=(
            "Path to split spec JSON. Must contain train_runs and test_runs "
            "(run stem names without .jsonl)."
        ),
    )
    ap.add_argument(
        "--out_dir",
        type=str,
        default="data/processed/jsonl",
        help="Base output directory.",
    )
    ap.add_argument(
        "--task_name",
        type=str,
        default="gridsybil_pseudo_ident_v1",
        help="Task folder name under out_dir.",
    )
    ap.add_argument(
        "--val_receiver_fraction",
        type=float,
        default=0.10,
        help="Validation receiver fraction sampled within each train run.",
    )
    ap.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for receiver sampling.",
    )
    return ap.parse_args()


def load_split_spec(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"split_json not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if "train_runs" not in payload or "test_runs" not in payload:
        raise ValueError("split_json must contain keys: train_runs, test_runs")
    train_runs = payload["train_runs"]
    test_runs = payload["test_runs"]
    if not isinstance(train_runs, list) or not isinstance(test_runs, list):
        raise ValueError("train_runs and test_runs must be lists")
    if not train_runs:
        raise ValueError("train_runs is empty")
    if not test_runs:
        raise ValueError("test_runs is empty")
    overlap = set(train_runs).intersection(set(test_runs))
    if overlap:
        raise ValueError(f"train_runs and test_runs overlap: {sorted(overlap)}")
    return payload


def build_run_file_map(episodes_dir: Path) -> Dict[str, Path]:
    if not episodes_dir.exists():
        raise FileNotFoundError(f"episodes_dir not found: {episodes_dir}")
    mapping: Dict[str, Path] = {}
    for p in sorted(episodes_dir.glob("*.jsonl")):
        mapping[p.stem] = p
    if not mapping:
        raise ValueError(f"No .jsonl files found in {episodes_dir}")
    return mapping


def iter_episode_records(path: Path) -> Iterator[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"JSON decode error in {path} at line {line_no}: {exc}") from exc


def scenario_family_from_attack_type(attack_type: Any) -> str:
    s = str(attack_type)
    if "0709" in s:
        return "0709"
    if "1416" in s:
        return "1416"
    return "unknown"


def extract_receiver_id(ep: Dict[str, Any]) -> int:
    return int(ep["meta"]["receiver_id"])


def normalize_pseudo_local_ids(ids: List[Any]) -> List[str]:
    parsed: List[Tuple[int, str]] = []
    seen: Set[str] = set()
    for raw in ids:
        s = str(raw).strip().lower()
        m = PSEUDO_LOCAL_ID_RE.match(s)
        if not m:
            continue
        if s in seen:
            continue
        seen.add(s)
        parsed.append((int(m.group(1)), s))
    parsed.sort(key=lambda t: t[0])
    return [s for _, s in parsed]


def get_candidate_pseudo_ids(ep: Dict[str, Any]) -> List[str]:
    entities = ep.get("pseudo_entities", [])
    ids = [ent.get("pseudo_local_id") for ent in entities if isinstance(ent, dict)]
    return normalize_pseudo_local_ids(ids)


def build_model_input(ep: Dict[str, Any]) -> Dict[str, Any]:
    meta = ep.get("meta", {})
    compact_meta = {
        "traffic_regime": meta.get("traffic_regime"),
        "window": meta.get("window", {}),
    }
    return {
        "task": "gridsybil_pseudo_ident_v1",
        "meta": compact_meta,
        "ego": ep.get("ego", {}),
        "region": ep.get("region", {}),
        "pseudo_entities": ep.get("pseudo_entities", []),
        "candidate_pseudo_local_ids": get_candidate_pseudo_ids(ep),
    }


def build_sample_from_episode(ep: Dict[str, Any]) -> Dict[str, Any]:
    meta = ep.get("meta", {})
    label_identity = ep.get("label", {}).get("identity", {})
    run_id = str(meta.get("run_id", "UNKNOWN_RUN"))
    receiver_id = int(meta.get("receiver_id", -1))
    attack_type = str(meta.get("attack_type", "UNKNOWN_ATTACK_TYPE"))
    scenario_family = scenario_family_from_attack_type(attack_type)
    window = meta.get("window", {})
    start = window.get("start", "NA")
    end = window.get("end", "NA")

    candidate_ids = get_candidate_pseudo_ids(ep)
    candidate_set = set(candidate_ids)
    true_ids = normalize_pseudo_local_ids(
        label_identity.get("attacker_pseudo_local_ids", [])
    )
    true_set = set(true_ids)
    if not true_set.issubset(candidate_set):
        missing = sorted(true_set - candidate_set)
        raise ValueError(
            f"True attacker pseudo IDs not in candidate list for run={run_id}, "
            f"receiver={receiver_id}, missing={missing}"
        )

    sample_id = f"{run_id}|rcv{receiver_id}|{start}-{end}"
    instruction = (
        "Identify attacker-controlled pseudo local IDs in this episode. "
        "Output ONLY a JSON array of pseudo local IDs, such as [\"p1\", \"p4\"]."
    )

    return {
        "id": sample_id,
        "instruction": instruction,
        "input": build_model_input(ep),
        "output": json.dumps(true_ids, ensure_ascii=False),
        "output_ids": true_ids,
        "eval_meta": {
            "run_id": run_id,
            "receiver_id": receiver_id,
            "attack_type": attack_type,
            "scenario_family": scenario_family,
            "num_candidates": len(candidate_ids),
            "num_true_attackers": len(true_ids),
        },
    }


def collect_receivers_for_run(path: Path) -> List[int]:
    receiver_ids: Set[int] = set()
    for ep in iter_episode_records(path):
        receiver_ids.add(extract_receiver_id(ep))
    return sorted(receiver_ids)


def sample_val_receivers_for_train_runs(
    train_runs: List[str],
    run_file_map: Dict[str, Path],
    val_receiver_fraction: float,
    seed: int,
) -> Dict[str, Set[int]]:
    if not (0.0 < val_receiver_fraction < 1.0):
        raise ValueError(
            f"val_receiver_fraction must be in (0, 1), got {val_receiver_fraction}"
        )

    rng = random.Random(seed)
    out: Dict[str, Set[int]] = {}
    for run in train_runs:
        receivers = collect_receivers_for_run(run_file_map[run])
        if not receivers:
            out[run] = set()
            continue
        shuffled = list(receivers)
        rng.shuffle(shuffled)
        k = max(1, int(len(shuffled) * val_receiver_fraction))
        out[run] = set(shuffled[:k])
    return out


def run_stem_from_meta_run_id(meta_run_id: str) -> str:
    # Example:
    # meta.run_id = "GridSybil_0709/VeReMi_25200_28800_2025-11-15_13_57_9"
    # stem       = "GridSybil_0709_VeReMi_25200_28800_2025-11-15_13_57_9"
    return meta_run_id.replace("/", "_")


def decide_split(
    ep: Dict[str, Any],
    train_runs: Set[str],
    test_runs: Set[str],
    val_receivers_by_run: Dict[str, Set[int]],
) -> str:
    run_id = str(ep.get("meta", {}).get("run_id", ""))
    run_stem = run_stem_from_meta_run_id(run_id)
    receiver_id = extract_receiver_id(ep)

    if run_stem in test_runs:
        return "test"
    if run_stem in train_runs:
        if receiver_id in val_receivers_by_run.get(run_stem, set()):
            return "val"
        return "train"
    raise ValueError(f"Episode run not in split spec: run_id={run_id}, stem={run_stem}")


def summarize_split_stats(stats: Dict[str, Any]) -> Dict[str, Any]:
    n = stats["n_samples"]
    avg_candidates = (
        stats["sum_candidates"] / n if n > 0 else 0.0
    )
    avg_true = (
        stats["sum_true_attackers"] / n if n > 0 else 0.0
    )
    empty_target_fraction = (
        stats["n_empty_targets"] / n if n > 0 else 0.0
    )
    return {
        "n_samples": n,
        "n_empty_targets": stats["n_empty_targets"],
        "empty_target_fraction": round(empty_target_fraction, 6),
        "avg_num_candidates": round(avg_candidates, 6),
        "avg_num_true_attackers": round(avg_true, 6),
        "samples_per_run_stem": dict(sorted(stats["samples_per_run_stem"].items())),
        "samples_per_family": dict(sorted(stats["samples_per_family"].items())),
    }


def init_stats() -> Dict[str, Any]:
    return {
        "n_samples": 0,
        "n_empty_targets": 0,
        "sum_candidates": 0,
        "sum_true_attackers": 0,
        "samples_per_run_stem": defaultdict(int),
        "samples_per_family": defaultdict(int),
    }


def update_stats(stats: Dict[str, Any], sample: Dict[str, Any]) -> None:
    stats["n_samples"] += 1
    n_true = int(sample["eval_meta"]["num_true_attackers"])
    n_candidates = int(sample["eval_meta"]["num_candidates"])
    if n_true == 0:
        stats["n_empty_targets"] += 1
    stats["sum_candidates"] += n_candidates
    stats["sum_true_attackers"] += n_true
    run_stem = run_stem_from_meta_run_id(sample["eval_meta"]["run_id"])
    family = sample["eval_meta"]["scenario_family"]
    stats["samples_per_run_stem"][run_stem] += 1
    stats["samples_per_family"][family] += 1


def ensure_runs_exist(run_ids: List[str], run_file_map: Dict[str, Path], tag: str) -> None:
    missing = [r for r in run_ids if r not in run_file_map]
    if missing:
        raise FileNotFoundError(f"{tag} runs missing episode JSONL files: {missing}")


def main() -> None:
    args = parse_args()

    episodes_dir = Path(args.episodes_dir)
    split_json = Path(args.split_json)
    out_base = Path(args.out_dir) / args.task_name
    out_base.mkdir(parents=True, exist_ok=True)

    split_spec = load_split_spec(split_json)
    run_file_map = build_run_file_map(episodes_dir)

    train_runs: List[str] = list(split_spec["train_runs"])
    test_runs: List[str] = list(split_spec["test_runs"])
    ensure_runs_exist(train_runs, run_file_map, tag="train")
    ensure_runs_exist(test_runs, run_file_map, tag="test")

    val_receivers_by_run = sample_val_receivers_for_train_runs(
        train_runs=train_runs,
        run_file_map=run_file_map,
        val_receiver_fraction=args.val_receiver_fraction,
        seed=args.seed,
    )

    out_paths = {
        "train": out_base / "train.jsonl",
        "val": out_base / "val.jsonl",
        "test": out_base / "test.jsonl",
    }
    for p in out_paths.values():
        p.parent.mkdir(parents=True, exist_ok=True)

    stats = {k: init_stats() for k in ["train", "val", "test"]}

    train_run_set = set(train_runs)
    test_run_set = set(test_runs)
    all_runs = sorted(set(train_runs + test_runs))

    with (
        out_paths["train"].open("w", encoding="utf-8") as f_train,
        out_paths["val"].open("w", encoding="utf-8") as f_val,
        out_paths["test"].open("w", encoding="utf-8") as f_test,
    ):
        handles = {"train": f_train, "val": f_val, "test": f_test}
        for run in all_runs:
            path = run_file_map[run]
            n_in = 0
            n_out = {"train": 0, "val": 0, "test": 0}
            for ep in iter_episode_records(path):
                n_in += 1
                split_name = decide_split(
                    ep=ep,
                    train_runs=train_run_set,
                    test_runs=test_run_set,
                    val_receivers_by_run=val_receivers_by_run,
                )
                sample = build_sample_from_episode(ep)
                handles[split_name].write(json.dumps(sample, ensure_ascii=False) + "\n")
                update_stats(stats[split_name], sample)
                n_out[split_name] += 1
            print(
                f"[run] {run}: in={n_in} -> train={n_out['train']} val={n_out['val']} test={n_out['test']}",
                flush=True,
            )

    split_summary = {k: summarize_split_stats(v) for k, v in stats.items()}
    manifest = {
        "task_name": args.task_name,
        "episodes_dir": str(episodes_dir),
        "split_json": str(split_json),
        "seed": args.seed,
        "val_receiver_fraction": args.val_receiver_fraction,
        "train_runs": train_runs,
        "test_runs": test_runs,
        "val_receivers_by_run": {
            run: sorted(list(receiver_ids))
            for run, receiver_ids in sorted(val_receivers_by_run.items())
        },
        "summary": split_summary,
        "outputs": {k: str(v) for k, v in out_paths.items()},
    }
    manifest_path = out_base / "split_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== Split summary ===", flush=True)
    for split_name in ["train", "val", "test"]:
        s = split_summary[split_name]
        print(
            f"[{split_name}] n={s['n_samples']} "
            f"empty_target_fraction={s['empty_target_fraction']:.4f} "
            f"avg_candidates={s['avg_num_candidates']:.2f} "
            f"avg_true_attackers={s['avg_num_true_attackers']:.2f}",
            flush=True,
        )
    print(f"Manifest written to: {manifest_path}", flush=True)


if __name__ == "__main__":
    main()

