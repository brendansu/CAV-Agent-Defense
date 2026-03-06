#!/bin/bash
#SBATCH --job-name=veremi-eval-base
#SBATCH --partition=work1
#SBATCH --time=08:00:00
#SBATCH --mem=32gb
#SBATCH --cpus-per-task=8
#SBATCH --gpus-per-node=a100:1
#SBATCH --array=0-7
#SBATCH --output=/scratch/%u/veremi_agent/slurm/eval_1of8_base_%A_%a.out
#SBATCH --error=/scratch/%u/veremi_agent/slurm/eval_1of8_base_%A_%a.err
#
# Evaluate Phase 1 binary (1of8) on TEST split using base model only.
# By default uses:
#   JSONL_DIR=$HOME/veremi_agent/data/processed/jsonl/phase1_binary

JSONL_DIR="${JSONL_DIR:-$HOME/veremi_agent/data/processed/jsonl/phase1_binary_1of8}"
NUM_SHARDS=8
SHARD_INDEX=${SLURM_ARRAY_TASK_ID}

module load cuda/12.3.0
module load anaconda3/2023.09-0
source "$(conda info --base)/etc/profile.d/conda.sh"
source activate veremi

cd "$HOME/veremi_agent"
echo "JSONL_DIR=$JSONL_DIR"
echo "NUM_SHARDS=$NUM_SHARDS"
echo "SHARD_INDEX=$SHARD_INDEX"

srun python -m src.eval.eval_phase1_binary \
  --jsonl_dir "$JSONL_DIR" \
  --split test \
  --mode base \
  --max_seq_len 1536 \
  --max_samples 0 \
  --show_examples 0 \
  --num_shards "$NUM_SHARDS" \
  --shard_index "$SHARD_INDEX"

