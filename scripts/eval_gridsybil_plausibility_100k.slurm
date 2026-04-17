#!/bin/bash
#SBATCH --job-name=veremi-eval-plausibility-100k
#SBATCH --partition=work1
#SBATCH --time=06:00:00
#SBATCH --mem=8gb
#SBATCH --cpus-per-task=2
#SBATCH --gpus-per-node=a100:1
#SBATCH --array=0-7
#SBATCH --output=/scratch/%u/veremi_collusion/slurm/eval_plausibility_lora_100k/log_%A_%a.out
#SBATCH --error=/scratch/%u/veremi_collusion/slurm/eval_plausibility_lora_100k/log_%A_%a.err

set -euo pipefail

WORK_DIR="${WORK_DIR:-$HOME/veremi_collusion}"
CONFIG="${CONFIG:-$HOME/veremi_collusion_local/qwen2.5_7b_gridsybil_plausibility_100k.yaml}"
SPLIT="${SPLIT:-test}"  # val / test
LORA_DIR="${LORA_DIR:-/scratch/$USER/veremi_collusion/outputs/qwen2.5-7b-gridsybil-plausibility-100k}"
EXP_NAME="${EXP_NAME:-100k}"
RUN_ID="${RUN_ID:-$(date +%Y%m%d-%H%M%S)_${EXP_NAME}}"

NUM_SHARDS=8
SHARD_INDEX=${SLURM_ARRAY_TASK_ID}

# 0 = all samples in the split (after sharding)
MAX_SAMPLES="${MAX_SAMPLES:-0}"
# empty means use reserve_answer_tokens from YAML
MAX_NEW_TOKENS="${MAX_NEW_TOKENS:-}"
LOG_ROW_PATH="${LOG_ROW_PATH:-/scratch/$USER/veremi_collusion/outputs/eval_logs/plausibility_100k/shard{shard_index}.jsonl}"
PROGRESS_EVERY_SAMPLES="${PROGRESS_EVERY_SAMPLES:-500}"

module load cuda/12.3.0
module load anaconda3/2023.09-0
source "$(conda info --base)/etc/profile.d/conda.sh"
source activate veremi

cd "$WORK_DIR"

echo "WORK_DIR=$WORK_DIR"
echo "CONFIG=$CONFIG"
echo "SPLIT=$SPLIT"
echo "LORA_DIR=$LORA_DIR"
echo "RUN_ID=$RUN_ID"
echo "NUM_SHARDS=$NUM_SHARDS"
echo "SHARD_INDEX=$SHARD_INDEX"
echo "MAX_SAMPLES=$MAX_SAMPLES"
echo "MAX_NEW_TOKENS=$MAX_NEW_TOKENS"
echo "LOG_ROW_PATH=$LOG_ROW_PATH"
echo "PROGRESS_EVERY_SAMPLES=$PROGRESS_EVERY_SAMPLES"

if [[ "$SPLIT" != "val" && "$SPLIT" != "test" ]]; then
  echo "[ERROR] SPLIT must be val or test, got: $SPLIT" >&2
  exit 2
fi

if [[ -n "$MAX_NEW_TOKENS" ]]; then
  srun python -m src.eval.eval_gridsybil_plausibility \
    --config "$CONFIG" \
    --mode lora \
    --lora_dir "$LORA_DIR" \
    --split "$SPLIT" \
    --max_samples "$MAX_SAMPLES" \
    --print_predictions 0 \
    --show_examples 0 \
    --num_shards "$NUM_SHARDS" \
    --shard_index "$SHARD_INDEX" \
    --run_id "$RUN_ID" \
    --log_row_path "$LOG_ROW_PATH" \
    --progress_every_samples "$PROGRESS_EVERY_SAMPLES" \
    --max_new_tokens "$MAX_NEW_TOKENS"
else
  srun python -m src.eval.eval_gridsybil_plausibility \
    --config "$CONFIG" \
    --mode lora \
    --lora_dir "$LORA_DIR" \
    --split "$SPLIT" \
    --max_samples "$MAX_SAMPLES" \
    --print_predictions 0 \
    --show_examples 0 \
    --num_shards "$NUM_SHARDS" \
    --shard_index "$SHARD_INDEX" \
    --run_id "$RUN_ID" \
    --log_row_path "$LOG_ROW_PATH" \
    --progress_every_samples "$PROGRESS_EVERY_SAMPLES"
fi