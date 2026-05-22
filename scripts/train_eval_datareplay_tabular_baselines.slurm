#!/bin/bash
#SBATCH --job-name=veremi-datareplay-tabular-baselines
#SBATCH --partition=work1
#SBATCH --time=01:00:00
#SBATCH --mem=16gb
#SBATCH --cpus-per-task=8
#SBATCH --output=/scratch/%u/veremi_collusion/slurm/train_eval_datareplay_tabular_baselines/log_%j.out
#SBATCH --error=/scratch/%u/veremi_collusion/slurm/train_eval_datareplay_tabular_baselines/log_%j.err

set -euo pipefail
export SLURM_EXPORT_ENV=ALL

WORK_DIR="${WORK_DIR:-$HOME/veremi_collusion}"
CONFIG="${CONFIG:-$HOME/veremi_collusion_local/qwen2.5_7b_datareplay_plausibility_recentk_autoq.yaml}"
MODEL="${MODEL:-both}"            # lr / gnb / rf / xgb / lgbm / mlp / svm / both / all / all_wo_svm
EVAL_SPLIT="${EVAL_SPLIT:-test}"  # val / test
RUN_ID="${RUN_ID:-$(date +%Y%m%d)_datareplay_tabular_baselines}"
OUTPUT_JSON="${OUTPUT_JSON:-/scratch/$USER/veremi_collusion/outputs/tabular_baselines/${RUN_ID}.json}"

module load anaconda3/2023.09-0
source "$(conda info --base)/etc/profile.d/conda.sh"
source activate veremi

cd "$WORK_DIR"

echo "WORK_DIR=$WORK_DIR"
echo "CONFIG=$CONFIG"
echo "MODEL=$MODEL"
echo "EVAL_SPLIT=$EVAL_SPLIT"
echo "RUN_ID=$RUN_ID"
echo "OUTPUT_JSON=$OUTPUT_JSON"

srun python -m src.baselines.train_eval_tabular_plausibility \
  --config "$CONFIG" \
  --model "$MODEL" \
  --eval_split "$EVAL_SPLIT" \
  --run_id "$RUN_ID" \
  --output_json "$OUTPUT_JSON"
