#!/bin/bash
#SBATCH --job-name=veremi-eval-gridsybil
#SBATCH --partition=work1
#SBATCH --time=08:00:00
#SBATCH --mem=32gb
#SBATCH --cpus-per-task=1
#SBATCH --gpus-per-node=a100:1
#SBATCH --array=0-7
#SBATCH --output=/scratch/%u/veremi_collusion/slurm/eval_gridsybil_%A_%a.out
#SBATCH --error=/scratch/%u/veremi_collusion/slurm/eval_gridsybil_%A_%a.err

# Evaluate GridSybil pseudo-entity identification on a split,
# sharded across array tasks. Supports both base and lora modes.
# Typical usage (same RUN_ID for one experiment batch):
#   RUN_ID=20260324_eval_v1
#   sbatch --export=ALL,SPLIT=val,MODEL_TAG=base,RUN_ID=$RUN_ID scripts/eval_gridsybil_pseudo_ident_lora.slurm
#   sbatch --export=ALL,SPLIT=val,MODEL_TAG=lora,RUN_ID=$RUN_ID scripts/eval_gridsybil_pseudo_ident_lora.slurm
#   sbatch --export=ALL,SPLIT=test,MODEL_TAG=base,RUN_ID=$RUN_ID scripts/eval_gridsybil_pseudo_ident_lora.slurm
#   sbatch --export=ALL,SPLIT=test,MODEL_TAG=lora,RUN_ID=$RUN_ID scripts/eval_gridsybil_pseudo_ident_lora.slurm

CONFIG="${CONFIG:-$HOME/veremi_collusion_local/qwen2.5_1.5b_gridsybil_pseudo_ident_hpc.yaml}"
SPLIT="${SPLIT:-test}"                 # val / test
MODEL_TAG="${MODEL_TAG:-lora}"         # base / lora
LORA_DIR="${LORA_DIR:-/scratch/$USER/veremi_collusion/outputs/qwen2.5-1.5b-pseudo-ident-balanced-a}"
RUN_ID="${RUN_ID:-$(date +%Y%m%d-%H%M%S)}"

NUM_SHARDS=8
SHARD_INDEX=${SLURM_ARRAY_TASK_ID}

# 0 = all samples in the split (sharded)
MAX_SAMPLES="${MAX_SAMPLES:-0}"

# By default eval_gridsybil_pseudo_ident.py will use reserve_answer_tokens from YAML.
# You can override if you want, otherwise keep as empty.
MAX_NEW_TOKENS="${MAX_NEW_TOKENS:-}"
LOG_TIERS="${LOG_TIERS:-L0,L1}"
LOG_PSEUDO_PATH="${LOG_PSEUDO_PATH:-/scratch/$USER/veremi_collusion/outputs/eval_logs/pseudo_fact_${RUN_ID}_${MODEL_TAG}_${SPLIT}_shard${SHARD_INDEX}.jsonl}"

module load cuda/12.3.0
module load anaconda3/2023.09-0
source "$(conda info --base)/etc/profile.d/conda.sh"
source activate veremi

cd "$HOME/veremi_collusion"

echo "CONFIG=$CONFIG"
echo "SPLIT=$SPLIT"
echo "MODEL_TAG=$MODEL_TAG"
echo "LORA_DIR=$LORA_DIR"
echo "RUN_ID=$RUN_ID"
echo "NUM_SHARDS=$NUM_SHARDS"
echo "SHARD_INDEX=$SHARD_INDEX"
echo "MAX_SAMPLES=$MAX_SAMPLES"
echo "MAX_NEW_TOKENS=$MAX_NEW_TOKENS"
echo "LOG_TIERS=$LOG_TIERS"
echo "LOG_PSEUDO_PATH=$LOG_PSEUDO_PATH"

if [[ "$MODEL_TAG" != "base" && "$MODEL_TAG" != "lora" ]]; then
  echo "[ERROR] MODEL_TAG must be base or lora, got: $MODEL_TAG" >&2
  exit 2
fi
if [[ "$SPLIT" != "val" && "$SPLIT" != "test" ]]; then
  echo "[ERROR] SPLIT must be val or test, got: $SPLIT" >&2
  exit 2
fi

COMMON_ARGS=(
  --config "$CONFIG"
  --split "$SPLIT"
  --max_samples "$MAX_SAMPLES"
  --print_predictions 0
  --show_examples 0
  --num_shards "$NUM_SHARDS"
  --shard_index "$SHARD_INDEX"
  --run_id "$RUN_ID"
  --log_tiers "$LOG_TIERS"
  --log_pseudo_path "$LOG_PSEUDO_PATH"
)

if [[ -n "$MAX_NEW_TOKENS" ]]; then
  COMMON_ARGS+=(--max_new_tokens "$MAX_NEW_TOKENS")
fi

if [[ "$MODEL_TAG" == "base" ]]; then
  srun python -m src.eval.eval_gridsybil_pseudo_ident \
    --mode base \
    "${COMMON_ARGS[@]}"
else
  srun python -m src.eval.eval_gridsybil_pseudo_ident \
    --mode lora \
    --lora_dir "$LORA_DIR" \
    "${COMMON_ARGS[@]}"
fi