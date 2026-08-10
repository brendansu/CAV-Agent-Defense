"""
Generate per-seed Slurm submit scripts for the dataset-sub-sampling seed sweep.

Background
----------
The existing benchmark submitters (`foundation_model_benchmark_submit_qwen_mistral.slurm`,
`foundation_model_benchmark_submit_llama_gemma.slurm`, `tabular_baseline_benchmark_submit.slurm`)
run every model against the single `seed=42` sampled dataset produced by
`src/data/sample_plausibility_parquet_splits.py`. To study sensitivity to the dataset
sub-sampling seed, we rerun the same pipeline against N additional seeds, each with its
own sampled parquet directory (see `run_seed_sweep_sampling.*` / the `_seedNN` dataset
dirs under `data/processed/`).

This script emits one Slurm submit script per (model_group, seed) pair into
`scripts/seed_sweep/`:
  - submit_qwen_seed<N>.slurm
  - submit_llama_seed<N>.slurm
  - submit_mistral_seed<N>.slurm
  - submit_gemma_seed<N>.slurm
  - submit_ml_seed<N>.slurm

Each script loops over all 3 attack families (gridsybil, datareplay, dosdisruptive),
mirroring `submit_one()` in the existing bundled submit scripts. Only the seed changes:
- `subsample_seed` in the YAML configs and the tabular baseline `--seed` are left at
  their default (42) on purpose, so the dataset sub-sampling seed is the sole varying
  factor between runs.
- No new per-seed YAML configs are needed for the 4 LLM families: the existing train/eval
  Slurm scripts already support a `PARQUET_DIR` env var that gets translated into a
  `--set parquet_dir=...` override on top of the checked-in YAML.
- The tabular baseline script now accepts a `--parquet_dir` CLI override (see
  `src/baselines/train_eval_tabular_plausibility.py` and
  `scripts/train_eval_datareplay_tabular_baselines.slurm`), which the generated
  `submit_ml_seed<N>.slurm` scripts use via a `PARQUET_DIR` export.

Usage
-----
    python scripts/generate_seed_sweep_submit_scripts.py

Re-run any time the SEEDS list changes; it overwrites files under scripts/seed_sweep/.
"""

from __future__ import annotations

from pathlib import Path

SEEDS = [2, 18, 25, 37, 45, 57, 65, 75, 80, 98]

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "scripts" / "seed_sweep"

# --- Per-attack, per-model metadata (mirrors foundation_model_benchmark_submit*.slurm) ---

ATTACKS = ["gridsybil", "datareplay", "dosdisruptive"]

# Base (seed=42) parquet dirs on HPC scratch, shared across all LLM models for a given attack.
PARQUET_DIR_BASE = {
    "gridsybil": "/scratch/$USER/veremi_collusion/data/gridsybil_plausibility_senderk_50k",
    "datareplay": "/scratch/$USER/veremi_collusion/data/datareplay_plausibility_senderk_autoq_50k",
    "dosdisruptive": "/scratch/$USER/veremi_collusion/data/dosdisruptive_plausibility_senderk_autoq_classic_50k",
}

EVAL_SCRIPT = {
    "gridsybil": "scripts/eval_gridsybil_plausibility_recentk_autoq_t1.slurm",
    "datareplay": "scripts/eval_datareplay_plausibility_recentk_autoq_t1.slurm",
    "dosdisruptive": "scripts/eval_dosdisruptive_plausibility_recentk_autoq_classic.slurm",
}

TRAIN_SCRIPT = {
    "gridsybil": "scripts/train_gridsybil_plausibility_recentk_autoq_t1.slurm",
    "datareplay": "scripts/train_datareplay_plausibility_recentk_autoq_t1.slurm",
    "dosdisruptive": "scripts/train_dosdisruptive_plausibility_recentk_autoq_classic.slurm",
}

TRAIN_SCRIPT_GEMMA = {
    "gridsybil": "scripts/train_gridsybil_plausibility_recentk_autoq_t1_gemma.slurm",
    "datareplay": "scripts/train_datareplay_plausibility_recentk_autoq_t1_gemma.slurm",
    "dosdisruptive": "scripts/train_dosdisruptive_plausibility_recentk_autoq_classic_gemma.slurm",
}

CONFIG_NAME = {
    "gridsybil": "gridsybil_plausibility_recentk_autoq_t1.yaml",
    "datareplay": "datareplay_plausibility_recentk_autoq.yaml",
    "dosdisruptive": "dosdisruptive_plausibility_recentk_autoq_classic.yaml",
}

EXP_NAME_SUFFIX = {
    "gridsybil": "gridsybil_recentk_autoq_t1",
    "datareplay": "datareplay_recentk_autoq",
    "dosdisruptive": "dosdisruptive_recentk_autoq_classic",
}

OUTPUT_NAME_SUFFIX = {
    "gridsybil": "gridsybil-plausibility-recentk-autoq-t1",
    "datareplay": "datareplay-plausibility-recentk-autoq",
    "dosdisruptive": "dosdisruptive-plausibility-recentk-autoq-classic",
}

# model_key -> (config_prefix_underscore, config_prefix_hyphen, uses_gemma_train_script)
LLM_MODELS = {
    "qwen": ("qwen2.5-7b", "qwen2.5_7b", "qwen2.5-7b", False),
    "llama": ("llama3.1-8b", "llama3.1_8b", "llama3.1-8b", False),
    "mistral": ("mistral-7b", "mistral_7b", "mistral-7b", False),
    "gemma": ("gemma2-9b", "gemma2_9b", "gemma2-9b", True),
}
# tuple layout: (model_key_for_submit_one, config_underscore_prefix, output_hyphen_prefix, uses_gemma_train_script)


SUBMIT_ONE_FN = '''\
extract_job_id() {
  awk '{print $4}'
}

submit_one() {
  local model_key="$1"
  local attack="$2"
  local config_name="$3"
  local train_script="$4"
  local eval_script="$5"
  local exp_name="$6"
  local output_name="$7"
  local parquet_dir="$8"

  local config_path="$CONFIG_ROOT/$config_name"
  local output_dir="/scratch/$USER/veremi_collusion/outputs/foundation/$output_name"
  local run_id="${DATE_TAG}_${exp_name}"

  if [[ ! -f "$config_path" ]]; then
    echo "[ERROR] Missing config: $config_path" >&2
    exit 2
  fi
  if [[ ! -f "$train_script" ]]; then
    echo "[ERROR] Missing train script: $train_script" >&2
    exit 2
  fi
  if [[ ! -f "$eval_script" ]]; then
    echo "[ERROR] Missing eval script: $eval_script" >&2
    exit 2
  fi

  local train_submit
  train_submit=$(sbatch \\
    --export=ALL,WORK_DIR="$WORK_DIR",CONFIG="$config_path",OUTPUT_DIR="$output_dir",PARQUET_DIR="$parquet_dir" \\
    "$train_script")
  local train_job
  train_job="$(echo "$train_submit" | extract_job_id)"

  local eval_submit
  eval_submit=$(sbatch \\
    --dependency=afterok:${train_job} \\
    --export=ALL,WORK_DIR="$WORK_DIR",CONFIG="$config_path",MODE="$MODE",LORA_DIR="$output_dir",SPLIT="$SPLIT",EXP_NAME="$exp_name",RUN_ID="$run_id",MAX_SAMPLES="$MAX_SAMPLES",NUM_SHARDS="$NUM_SHARDS",PARQUET_DIR="$parquet_dir" \\
    "$eval_script")
  local eval_job
  eval_job="$(echo "$eval_submit" | extract_job_id)"

  echo "[submit] model=$model_key attack=$attack seed=$SEED"
  echo "         config=$config_path"
  echo "         parquet_dir=$parquet_dir"
  echo "         output_dir=$output_dir"
  echo "         train_job=$train_job"
  echo "         eval_job=$eval_job depends_on=$train_job"
  echo "         run_id=$run_id"
}
'''


def render_llm_script(model_key: str, seed: int) -> str:
    label, config_underscore, output_hyphen, uses_gemma = LLM_MODELS[model_key]

    calls = []
    for attack in ATTACKS:
        config_name = f"{config_underscore}_{CONFIG_NAME[attack]}"
        train_script = (TRAIN_SCRIPT_GEMMA if uses_gemma else TRAIN_SCRIPT)[attack]
        eval_script = EVAL_SCRIPT[attack]
        exp_name = f"foundation_{config_underscore}_{EXP_NAME_SUFFIX[attack]}_seed{seed}"
        output_name = f"{output_hyphen}-{OUTPUT_NAME_SUFFIX[attack]}-seed{seed}"
        parquet_dir = f"{PARQUET_DIR_BASE[attack]}-seed{seed}"
        calls.append(
            f'submit_one "{label}" "{attack}" \\\n'
            f'  "{config_name}" \\\n'
            f'  "{train_script}" \\\n'
            f'  "{eval_script}" \\\n'
            f'  "{exp_name}" \\\n'
            f'  "{output_name}" \\\n'
            f'  "{parquet_dir}"\n'
        )
    calls_block = "\n".join(calls)

    return f'''#!/bin/bash
#SBATCH --job-name=veremi-submit-{model_key}-seed{seed}-benchmark
#SBATCH --partition=work1
#SBATCH --time=00:30:00
#SBATCH --mem=2gb
#SBATCH --cpus-per-task=1
#SBATCH --output=/scratch/%u/veremi_collusion/slurm/seed_sweep_submit/{model_key}_seed{seed}/log_%j.out
#SBATCH --error=/scratch/%u/veremi_collusion/slurm/seed_sweep_submit/{model_key}_seed{seed}/log_%j.err

set -euo pipefail
export SLURM_EXPORT_ENV=ALL

# Auto-generated by scripts/generate_seed_sweep_submit_scripts.py -- do not edit by hand.
# model={model_key} seed={seed}
# Submits gridsybil + datareplay + dosdisruptive train/eval jobs for this model against
# the seed={seed} sampled dataset (sample_plausibility_parquet_splits.py --seed {seed}).
# subsample_seed / model-side randomness are left at the YAML default (42).

WORK_DIR="${{WORK_DIR:-$HOME/veremi_collusion}}"
CONFIG_ROOT="${{CONFIG_ROOT:-$WORK_DIR/configs/llm_foundation}}"
SPLIT="${{SPLIT:-test}}"          # val / test
MODE="${{MODE:-lora}}"            # base / lora / both
MAX_SAMPLES="${{MAX_SAMPLES:-0}}" # 0 = all samples after sharding
NUM_SHARDS="${{NUM_SHARDS:-8}}"
SEED={seed}

DATE_TAG="$(date +%Y%m%d_%H%M%S)"

cd "$WORK_DIR"

echo "WORK_DIR=$WORK_DIR"
echo "CONFIG_ROOT=$CONFIG_ROOT"
echo "SPLIT=$SPLIT"
echo "MODE=$MODE"
echo "MAX_SAMPLES=$MAX_SAMPLES"
echo "NUM_SHARDS=$NUM_SHARDS"
echo "SEED=$SEED"
echo "DATE_TAG=$DATE_TAG"

{SUBMIT_ONE_FN}
{calls_block}
echo "[done] {model_key} seed={seed} benchmark jobs submitted."
'''


def render_ml_script(seed: int) -> str:
    return f'''#!/bin/bash
#SBATCH --job-name=veremi-submit-ml-seed{seed}-benchmark
#SBATCH --partition=work1
#SBATCH --time=00:20:00
#SBATCH --mem=2gb
#SBATCH --cpus-per-task=1
#SBATCH --output=/scratch/%u/veremi_collusion/slurm/seed_sweep_submit/ml_seed{seed}/log_%j.out
#SBATCH --error=/scratch/%u/veremi_collusion/slurm/seed_sweep_submit/ml_seed{seed}/log_%j.err

set -euo pipefail
export SLURM_EXPORT_ENV=ALL

# Auto-generated by scripts/generate_seed_sweep_submit_scripts.py -- do not edit by hand.
# tabular baselines, seed={seed}
# Submits gridsybil + datareplay + dosdisruptive tabular baseline jobs (all_wo_svm + svm)
# against the seed={seed} sampled dataset, via the --parquet_dir override added to
# src/baselines/train_eval_tabular_plausibility.py. The YAML CONFIG_* paths below are only
# used for feature-column definitions and are unchanged from the seed=42 setup; the
# --seed passed to the tabular models themselves stays at its default (42).

WORK_DIR="${{WORK_DIR:-$HOME/veremi_collusion}}"
BASELINE_JOB_SCRIPT="${{BASELINE_JOB_SCRIPT:-$WORK_DIR/scripts/train_eval_datareplay_tabular_baselines.slurm}}"
EVAL_SPLIT="${{EVAL_SPLIT:-test}}"   # val / test
SEED={seed}

DATE_TAG="$(date +%Y%m%d_%H%M%S)"

CONFIG_DATAREPLAY="${{CONFIG_DATAREPLAY:-$HOME/veremi_collusion_local/qwen2.5_7b_datareplay_plausibility_recentk_autoq.yaml}}"
CONFIG_DOS="${{CONFIG_DOS:-$HOME/veremi_collusion_local/qwen2.5_7b_dosdisruptive_plausibility_recentk_autoq_classic.yaml}}"
CONFIG_GRIDSYBIL="${{CONFIG_GRIDSYBIL:-$HOME/veremi_collusion_local/qwen2.5_7b_gridsybil_plausibility_recentk_autoq_t1.yaml}}"

PARQUET_DIR_DATAREPLAY="{PARQUET_DIR_BASE['datareplay']}-seed{seed}"
PARQUET_DIR_DOS="{PARQUET_DIR_BASE['dosdisruptive']}-seed{seed}"
PARQUET_DIR_GRIDSYBIL="{PARQUET_DIR_BASE['gridsybil']}-seed{seed}"

if [[ ! -f "$BASELINE_JOB_SCRIPT" ]]; then
  echo "[ERROR] BASELINE_JOB_SCRIPT not found: $BASELINE_JOB_SCRIPT" >&2
  exit 2
fi

cd "$WORK_DIR"

echo "WORK_DIR=$WORK_DIR"
echo "BASELINE_JOB_SCRIPT=$BASELINE_JOB_SCRIPT"
echo "EVAL_SPLIT=$EVAL_SPLIT"
echo "SEED=$SEED"
echo "DATE_TAG=$DATE_TAG"
echo "CONFIG_DATAREPLAY=$CONFIG_DATAREPLAY"
echo "CONFIG_DOS=$CONFIG_DOS"
echo "CONFIG_GRIDSYBIL=$CONFIG_GRIDSYBIL"

submit_one() {{
  local attack="$1"
  local model_group="$2"
  local cfg="$3"
  local parquet_dir="$4"

  local run_id="${{DATE_TAG}}_${{attack}}_${{model_group}}_seed{seed}"
  local output_json="/scratch/$USER/veremi_collusion/outputs/tabular_baselines/${{run_id}}.json"

  local submit_out
  submit_out=$(sbatch --export=ALL,WORK_DIR="$WORK_DIR",CONFIG="$cfg",MODEL="$model_group",EVAL_SPLIT="$EVAL_SPLIT",RUN_ID="$run_id",OUTPUT_JSON="$output_json",PARQUET_DIR="$parquet_dir" "$BASELINE_JOB_SCRIPT")
  local job_id
  job_id="$(echo "$submit_out" | awk '{{print $4}}')"

  echo "[submit] attack=$attack model_group=$model_group seed=$SEED job_id=$job_id"
  echo "         run_id=$run_id"
  echo "         parquet_dir=$parquet_dir"
  echo "         output_json=$output_json"
}}

# 3 attack types x 2 model groups = 6 jobs
submit_one "datareplay" "all_wo_svm" "$CONFIG_DATAREPLAY" "$PARQUET_DIR_DATAREPLAY"
submit_one "datareplay" "svm" "$CONFIG_DATAREPLAY" "$PARQUET_DIR_DATAREPLAY"

submit_one "dosdisruptive" "all_wo_svm" "$CONFIG_DOS" "$PARQUET_DIR_DOS"
submit_one "dosdisruptive" "svm" "$CONFIG_DOS" "$PARQUET_DIR_DOS"

submit_one "gridsybil" "all_wo_svm" "$CONFIG_GRIDSYBIL" "$PARQUET_DIR_GRIDSYBIL"
submit_one "gridsybil" "svm" "$CONFIG_GRIDSYBIL" "$PARQUET_DIR_GRIDSYBIL"

echo "[done] ml seed={seed} tabular baseline jobs submitted."
'''


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    for seed in SEEDS:
        for model_key in ("qwen", "llama", "mistral", "gemma"):
            path = OUT_DIR / f"submit_{model_key}_seed{seed}.slurm"
            path.write_text(render_llm_script(model_key, seed), encoding="utf-8", newline="\n")
            written.append(path)
        path = OUT_DIR / f"submit_ml_seed{seed}.slurm"
        path.write_text(render_ml_script(seed), encoding="utf-8", newline="\n")
        written.append(path)

    print(f"[done] wrote {len(written)} scripts to {OUT_DIR}")
    for p in written:
        print(f"  {p.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
