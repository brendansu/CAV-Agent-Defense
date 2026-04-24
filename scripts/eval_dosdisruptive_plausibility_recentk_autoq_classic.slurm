#!/bin/bash
#SBATCH --job-name=veremi-dosdisruptive-plausibility-eval-classic
#SBATCH --partition=work1
#SBATCH --time=03:00:00
#SBATCH --mem=8gb
#SBATCH --cpus-per-task=2
#SBATCH --gpus-per-node=a100:1
#SBATCH --array=0-7
#SBATCH --output=/scratch/%u/veremi_collusion/slurm/eval_dosdisruptive_plausibility_recentk_autoq_classic/log_%A_%a.out
#SBATCH --error=/scratch/%u/veremi_collusion/slurm/eval_dosdisruptive_plausibility_recentk_autoq_classic/log_%A_%a.err

set -euo pipefail
export SLURM_EXPORT_ENV=ALL

WORK_DIR="${WORK_DIR:-$HOME/veremi_collusion}"
CONFIG="${CONFIG:-$HOME/veremi_collusion_local/qwen2.5_7b_dosdisruptive_plausibility_recentk_autoq_classic.yaml}"
MODE="${MODE:-lora}"   # base / lora / both
LORA_DIR="${LORA_DIR:-/scratch/$USER/veremi_collusion/outputs/qwen2.5-7b-dosdisruptive-plausibility-recentk-autoq-classic}"
SPLIT="${SPLIT:-test}" # val / test
EXP_NAME="${EXP_NAME:-dosdisruptive_autoq_classic}"
RUN_ID="${RUN_ID:-$(date +%Y%m%d)_${EXP_NAME}}"
PARQUET_DIR="${PARQUET_DIR:-}"

MAX_SAMPLES="${MAX_SAMPLES:-0}"      # 0 = all after sharding
MAX_NEW_TOKENS="${MAX_NEW_TOKENS:-}" # empty => reserve_answer_tokens from YAML
NUM_SHARDS="${NUM_SHARDS:-8}"
SHARD_INDEX="${SHARD_INDEX:-${SLURM_ARRAY_TASK_ID:-0}}"
LOG_ROW_PATH="${LOG_ROW_PATH:-/scratch/$USER/veremi_collusion/outputs/eval_logs/${RUN_ID}/shard${SHARD_INDEX}.jsonl.gz}"
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
echo "LORA_DIR=$LORA_DIR"
echo "SPLIT=$SPLIT"
echo "RUN_ID=$RUN_ID"
echo "PARQUET_DIR=$PARQUET_DIR"
echo "MAX_SAMPLES=$MAX_SAMPLES"
echo "MAX_NEW_TOKENS=$MAX_NEW_TOKENS"
echo "NUM_SHARDS=$NUM_SHARDS"
echo "SHARD_INDEX=$SHARD_INDEX"
echo "LOG_ROW_PATH=$LOG_ROW_PATH"
echo "PROGRESS_EVERY_SAMPLES=$PROGRESS_EVERY_SAMPLES"
echo "EVAL_OVERRIDES=$EVAL_OVERRIDES"

if [[ "$SPLIT" != "val" && "$SPLIT" != "test" ]]; then
  echo "[ERROR] SPLIT must be val or test, got: $SPLIT" >&2
  exit 2
fi
if [[ ("$MODE" == "lora" || "$MODE" == "both") && -z "$LORA_DIR" ]]; then
  echo "[ERROR] LORA_DIR is required when MODE=lora/both." >&2
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
