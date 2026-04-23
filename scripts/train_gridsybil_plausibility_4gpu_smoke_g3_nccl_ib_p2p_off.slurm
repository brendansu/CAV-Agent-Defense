#!/bin/bash
#SBATCH --job-name=veremi-plausibility-smoke-g3
#SBATCH --partition=work1
#SBATCH --time=01:00:00
#SBATCH --mem=80gb
#SBATCH --cpus-per-task=16
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --gpus-per-node=a100:4
#SBATCH --output=/scratch/%u/veremi_collusion/slurm/train_plausibility_smoke_g3/log_%j.out
#SBATCH --error=/scratch/%u/veremi_collusion/slurm/train_plausibility_smoke_g3/log_%j.err

set -euo pipefail
export SLURM_EXPORT_ENV=ALL

WORK_DIR="${WORK_DIR:-$HOME/veremi_collusion}"
CONFIG="${CONFIG:-$HOME/veremi_collusion_local/qwen2.5_7b_gridsybil_plausibility_recentk_50k.yaml}"
OUTPUT_DIR="${OUTPUT_DIR:-}"
TRAIN_OVERRIDES="${TRAIN_OVERRIDES:-}"

module load cuda/12.3.0
module load anaconda3/2023.09-0
source "$(conda info --base)/etc/profile.d/conda.sh"
source activate veremi

cd "$WORK_DIR"

export TOKENIZERS_PARALLELISM=false
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NCCL_DEBUG=INFO
export TORCH_DISTRIBUTED_DEBUG=DETAIL
export NCCL_IB_DISABLE=1
export NCCL_P2P_DISABLE=1

echo "=== Smoke G3 (NCCL IB + P2P disabled) ==="
echo "WORK_DIR=$WORK_DIR"
echo "CONFIG=$CONFIG"
echo "OUTPUT_DIR=$OUTPUT_DIR"
echo "TRAIN_OVERRIDES=$TRAIN_OVERRIDES"
echo "HOSTNAME=$(hostname)"
echo "CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES:-<unset>}"
echo "NCCL_DEBUG=$NCCL_DEBUG"
echo "TORCH_DISTRIBUTED_DEBUG=$TORCH_DISTRIBUTED_DEBUG"
echo "NCCL_IB_DISABLE=$NCCL_IB_DISABLE"
echo "NCCL_P2P_DISABLE=$NCCL_P2P_DISABLE"
python -V
ulimit -a

OVERRIDE_ARGS=()
if [[ -n "$TRAIN_OVERRIDES" ]]; then
  read -r -a _overrides <<< "$TRAIN_OVERRIDES"
  for kv in "${_overrides[@]}"; do
    OVERRIDE_ARGS+=(--set "$kv")
  done
fi

if [[ -n "$OUTPUT_DIR" ]]; then
  srun --ntasks=1 torchrun --standalone --nnodes=1 --nproc_per_node=4 --max_restarts=0 \
    -m src.training.train_lora_qwen_gridsybil_plausibility \
    --config "$CONFIG" \
    --output_dir "$OUTPUT_DIR" \
    "${OVERRIDE_ARGS[@]}"
else
  srun --ntasks=1 torchrun --standalone --nnodes=1 --nproc_per_node=4 --max_restarts=0 \
    -m src.training.train_lora_qwen_gridsybil_plausibility \
    --config "$CONFIG" \
    "${OVERRIDE_ARGS[@]}"
fi
