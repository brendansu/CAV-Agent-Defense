#!/bin/bash
#SBATCH --job-name=veremi-eval-plausibility-lora-recentk-autoq-t1-0.5b
#SBATCH --partition=work1
#SBATCH --time=01:00:00
#SBATCH --mem=8gb
#SBATCH --cpus-per-task=2
#SBATCH --gpus-per-node=a100:1
#SBATCH --array=0-7
#SBATCH --output=/scratch/%u/veremi_collusion/slurm/eval_plausibility_lora_recentk_autoq_t1_0.5b/log_%A_%a.out
#SBATCH --error=/scratch/%u/veremi_collusion/slurm/eval_plausibility_lora_recentk_autoq_t1_0.5b/log_%A_%a.err

set -euo pipefail
export SLURM_EXPORT_ENV=ALL

WORK_DIR="${WORK_DIR:-$HOME/veremi_collusion}"
CONFIG="${CONFIG:-$HOME/veremi_collusion_local/qwen2.5_0.5b_gridsybil_plausibility_recentk_autoq_t1.yaml}"
MODE="${MODE:-lora}"
SPLIT="${SPLIT:-test}"  # val / test
LORA_DIR="${LORA_DIR:-/scratch/$USER/veremi_collusion/outputs/qwen2.5-0.5b-gridsybil-plausibility-recentk-autoq-t1}"
EXP_NAME="${EXP_NAME:-recentk_autoq_t1_0.5b}"
RUN_ID="${RUN_ID:-$(date +%Y%m%d)_${EXP_NAME}}"
PARQUET_DIR="${PARQUET_DIR:-}"

NUM_SHARDS=8
SHARD_INDEX=${SLURM_ARRAY_TASK_ID}

MAX_SAMPLES="${MAX_SAMPLES:-0}"
MAX_NEW_TOKENS="${MAX_NEW_TOKENS:-}"
LOG_ROW_PATH="${LOG_ROW_PATH:-/scratch/$USER/veremi_collusion/outputs/eval_logs/plausibility_lora_recentk_autoq_t1_0.5b/shard${SHARD_INDEX}.jsonl.gz}"
PROGRESS_EVERY_SAMPLES="${PROGRESS_EVERY_SAMPLES:-500}"

EVAL_OVERRIDES="${EVAL_OVERRIDES:-}"

module load cuda/12.3.0
module load anaconda3/2023.09-0
source "$(conda info --base)/etc/profile.d/conda.sh"
source activate veremi

cd "$WORK_DIR"

echo "WORK_DIR=$WORK_DIR"
echo "CONFIG=$CONFIG"
echo "MODE=$MODE"
echo "SPLIT=$SPLIT"
echo "LORA_DIR=$LORA_DIR"
echo "PARQUET_DIR=$PARQUET_DIR"
echo "RUN_ID=$RUN_ID"
echo "NUM_SHARDS=$NUM_SHARDS"
echo "SHARD_INDEX=$SHARD_INDEX"
echo "MAX_SAMPLES=$MAX_SAMPLES"
echo "MAX_NEW_TOKENS=$MAX_NEW_TOKENS"
echo "LOG_ROW_PATH=$LOG_ROW_PATH"
echo "PROGRESS_EVERY_SAMPLES=$PROGRESS_EVERY_SAMPLES"
echo "EVAL_OVERRIDES=$EVAL_OVERRIDES"

if [[ "$SPLIT" != "val" && "$SPLIT" != "test" ]]; then
  echo "[ERROR] SPLIT must be val or test, got: $SPLIT" >&2
  exit 2
fi

if [[ ("$MODE" == "lora" || "$MODE" == "both") && -z "$LORA_DIR" ]]; then
  echo "[ERROR] LORA_DIR is required when MODE is lora/both." >&2
  exit 2
fi

OVERRIDE_ARGS=()
if [[ -n "$PARQUET_DIR" ]]; then
  OVERRIDE_ARGS+=(--set "parquet_dir=$PARQUET_DIR")
fi
if [[ -n "$EVAL_OVERRIDES" ]]; then
  read -r -a _overrides <<< "$EVAL_OVERRIDES"
  for kv in "${_overrides[@]}"; do
    OVERRIDE_ARGS+=(--set "$kv")
  done
fi

CMD=(
  python -m src.eval.eval_gridsybil_plausibility
  --config "$CONFIG"
  --mode "$MODE"
  --split "$SPLIT"
  --max_samples "$MAX_SAMPLES"
  --print_predictions 0
  --show_examples 0
  --num_shards "$NUM_SHARDS"
  --shard_index "$SHARD_INDEX"
  --run_id "$RUN_ID"
  --progress_every_samples "$PROGRESS_EVERY_SAMPLES"
)
if [[ -n "$LORA_DIR" ]]; then
  CMD+=(--lora_dir "$LORA_DIR")
fi
if [[ -n "$MAX_NEW_TOKENS" ]]; then
  CMD+=(--max_new_tokens "$MAX_NEW_TOKENS")
fi
if [[ -n "$LOG_ROW_PATH" ]]; then
  CMD+=(--log_row_path "$LOG_ROW_PATH")
fi
CMD+=("${OVERRIDE_ARGS[@]}")
srun "${CMD[@]}"
