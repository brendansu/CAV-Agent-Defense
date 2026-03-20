#!/bin/bash
#SBATCH --job-name=veremi-eval-gridsybil-base
#SBATCH --partition=work1
#SBATCH --time=08:00:00
#SBATCH --mem=32gb
#SBATCH --cpus-per-task=8
#SBATCH --gpus-per-node=a100:1
#SBATCH --array=0-7
#SBATCH --output=/scratch/%u/veremi_collusion/slurm/eval_gridsybil_base_%A_%a.out
#SBATCH --error=/scratch/%u/veremi_collusion/slurm/eval_gridsybil_base_%A_%a.err

# Evaluate GridSybil pseudo-entity identification on a split,
# sharded across array tasks. Metrics only (no per-sample prints).

CONFIG="${CONFIG:-$HOME/veremi_collusion_local/qwen2.5_1.5b_gridsybil_pseudo_ident_hpc.yaml}"
SPLIT="${SPLIT:-test}"                 # or test
MODE="${MODE:-base}"                 # base / lora / both
LORA_DIR="${LORA_DIR:-/scratch/$USER/veremi_collusion/outputs/qwen2.5-1.5b-pseudo-ident-balanced-a}"

NUM_SHARDS=8
SHARD_INDEX=${SLURM_ARRAY_TASK_ID}

# 0 = all samples in the split (sharded)
MAX_SAMPLES="${MAX_SAMPLES:-0}"

# By default eval_gridsybil_pseudo_ident.py will use reserve_answer_tokens from YAML.
# You can override if you want, otherwise keep as empty.
MAX_NEW_TOKENS="${MAX_NEW_TOKENS:-}"

module load cuda/12.3.0
module load anaconda3/2023.09-0
source "$(conda info --base)/etc/profile.d/conda.sh"
source activate veremi

cd "$HOME/veremi_collusion"

echo "CONFIG=$CONFIG"
echo "SPLIT=$SPLIT"
echo "MODE=$MODE"
echo "LORA_DIR=$LORA_DIR"
echo "NUM_SHARDS=$NUM_SHARDS"
echo "SHARD_INDEX=$SHARD_INDEX"
echo "MAX_SAMPLES=$MAX_SAMPLES"
echo "MAX_NEW_TOKENS=$MAX_NEW_TOKENS"

if [[ -n "$MAX_NEW_TOKENS" ]]; then
  srun python -m src.eval.eval_gridsybil_pseudo_ident \
    --config "$CONFIG" \
    --mode "$MODE" \
    --lora_dir "$LORA_DIR" \
    --split "$SPLIT" \
    --max_samples "$MAX_SAMPLES" \
    --print_predictions 0 \
    --show_examples 0 \
    --num_shards "$NUM_SHARDS" \
    --shard_index "$SHARD_INDEX" \
    --max_new_tokens "$MAX_NEW_TOKENS"
else
  srun python -m src.eval.eval_gridsybil_pseudo_ident \
    --config "$CONFIG" \
    --mode "$MODE" \
    --lora_dir "$LORA_DIR" \
    --split "$SPLIT" \
    --max_samples "$MAX_SAMPLES" \
    --print_predictions 0 \
    --show_examples 0 \
    --num_shards "$NUM_SHARDS" \
    --shard_index "$SHARD_INDEX"
fi