#!/bin/bash
#SBATCH --job-name=veremi-submit-foundation-qwen-mistral
#SBATCH --partition=work1
#SBATCH --time=00:30:00
#SBATCH --mem=2gb
#SBATCH --cpus-per-task=1
#SBATCH --output=/scratch/%u/veremi_collusion/slurm/foundation_model_benchmark_submit_qwen_mistral/log_%j.out
#SBATCH --error=/scratch/%u/veremi_collusion/slurm/foundation_model_benchmark_submit_qwen_mistral/log_%j.err

set -euo pipefail
export SLURM_EXPORT_ENV=ALL

WORK_DIR="${WORK_DIR:-$HOME/veremi_collusion}"
CONFIG_ROOT="${CONFIG_ROOT:-$WORK_DIR/configs/llm_foundation}"
SPLIT="${SPLIT:-test}"          # val / test
MODE="${MODE:-lora}"            # base / lora / both
MAX_SAMPLES="${MAX_SAMPLES:-0}" # 0 = all samples after sharding
NUM_SHARDS="${NUM_SHARDS:-8}"

DATE_TAG="$(date +%Y%m%d_%H%M%S)"

cd "$WORK_DIR"

echo "WORK_DIR=$WORK_DIR"
echo "CONFIG_ROOT=$CONFIG_ROOT"
echo "SPLIT=$SPLIT"
echo "MODE=$MODE"
echo "MAX_SAMPLES=$MAX_SAMPLES"
echo "NUM_SHARDS=$NUM_SHARDS"
echo "DATE_TAG=$DATE_TAG"

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
  train_submit=$(sbatch \
    --export=ALL,WORK_DIR="$WORK_DIR",CONFIG="$config_path",OUTPUT_DIR="$output_dir",PARQUET_DIR="$parquet_dir" \
    "$train_script")
  local train_job
  train_job="$(echo "$train_submit" | extract_job_id)"

  local eval_submit
  eval_submit=$(sbatch \
    --dependency=afterok:${train_job} \
    --export=ALL,WORK_DIR="$WORK_DIR",CONFIG="$config_path",MODE="$MODE",LORA_DIR="$output_dir",SPLIT="$SPLIT",EXP_NAME="$exp_name",RUN_ID="$run_id",MAX_SAMPLES="$MAX_SAMPLES",NUM_SHARDS="$NUM_SHARDS",PARQUET_DIR="$parquet_dir" \
    "$eval_script")
  local eval_job
  eval_job="$(echo "$eval_submit" | extract_job_id)"

  echo "[submit] model=$model_key attack=$attack"
  echo "         config=$config_path"
  echo "         parquet_dir=$parquet_dir"
  echo "         output_dir=$output_dir"
  echo "         train_job=$train_job"
  echo "         eval_job=$eval_job depends_on=$train_job"
  echo "         run_id=$run_id"
}

submit_one "qwen2.5-7b" "gridsybil" \
  "qwen2.5_7b_gridsybil_plausibility_recentk_autoq_t1.yaml" \
  "scripts/train_gridsybil_plausibility_recentk_autoq_t1.slurm" \
  "scripts/eval_gridsybil_plausibility_recentk_autoq_t1.slurm" \
  "foundation_qwen2.5_7b_gridsybil_recentk_autoq_t1" \
  "qwen2.5-7b-gridsybil-plausibility-recentk-autoq-t1" \
  "/scratch/$USER/veremi_collusion/data/gridsybil_plausibility_senderk_50k"

submit_one "mistral-7b" "gridsybil" \
  "mistral_7b_gridsybil_plausibility_recentk_autoq_t1.yaml" \
  "scripts/train_gridsybil_plausibility_recentk_autoq_t1.slurm" \
  "scripts/eval_gridsybil_plausibility_recentk_autoq_t1.slurm" \
  "foundation_mistral_7b_gridsybil_recentk_autoq_t1" \
  "mistral-7b-gridsybil-plausibility-recentk-autoq-t1" \
  "/scratch/$USER/veremi_collusion/data/gridsybil_plausibility_senderk_50k"

submit_one "qwen2.5-7b" "datareplay" \
  "qwen2.5_7b_datareplay_plausibility_recentk_autoq.yaml" \
  "scripts/train_datareplay_plausibility_recentk_autoq_t1.slurm" \
  "scripts/eval_datareplay_plausibility_recentk_autoq_t1.slurm" \
  "foundation_qwen2.5_7b_datareplay_recentk_autoq" \
  "qwen2.5-7b-datareplay-plausibility-recentk-autoq" \
  "/scratch/$USER/veremi_collusion/data/datareplay_plausibility_senderk_autoq_50k"

submit_one "mistral-7b" "datareplay" \
  "mistral_7b_datareplay_plausibility_recentk_autoq.yaml" \
  "scripts/train_datareplay_plausibility_recentk_autoq_t1.slurm" \
  "scripts/eval_datareplay_plausibility_recentk_autoq_t1.slurm" \
  "foundation_mistral_7b_datareplay_recentk_autoq" \
  "mistral-7b-datareplay-plausibility-recentk-autoq" \
  "/scratch/$USER/veremi_collusion/data/datareplay_plausibility_senderk_autoq_50k"

submit_one "qwen2.5-7b" "dosdisruptive" \
  "qwen2.5_7b_dosdisruptive_plausibility_recentk_autoq_classic.yaml" \
  "scripts/train_dosdisruptive_plausibility_recentk_autoq_classic.slurm" \
  "scripts/eval_dosdisruptive_plausibility_recentk_autoq_classic.slurm" \
  "foundation_qwen2.5_7b_dosdisruptive_recentk_autoq_classic" \
  "qwen2.5-7b-dosdisruptive-plausibility-recentk-autoq-classic" \
  "/scratch/$USER/veremi_collusion/data/dosdisruptive_plausibility_senderk_autoq_classic_50k"

submit_one "mistral-7b" "dosdisruptive" \
  "mistral_7b_dosdisruptive_plausibility_recentk_autoq_classic.yaml" \
  "scripts/train_dosdisruptive_plausibility_recentk_autoq_classic.slurm" \
  "scripts/eval_dosdisruptive_plausibility_recentk_autoq_classic.slurm" \
  "foundation_mistral_7b_dosdisruptive_recentk_autoq_classic" \
  "mistral-7b-dosdisruptive-plausibility-recentk-autoq-classic" \
  "/scratch/$USER/veremi_collusion/data/dosdisruptive_plausibility_senderk_autoq_classic_50k"

echo "[done] qwen + mistral foundation model benchmark jobs submitted."
