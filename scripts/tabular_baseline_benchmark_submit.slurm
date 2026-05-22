#!/bin/bash
#SBATCH --job-name=veremi-submit-tabular-baselines
#SBATCH --partition=work1
#SBATCH --time=00:20:00
#SBATCH --mem=2gb
#SBATCH --cpus-per-task=1
#SBATCH --output=/scratch/%u/veremi_collusion/slurm/tabular_baseline_submit/log_%j.out
#SBATCH --error=/scratch/%u/veremi_collusion/slurm/tabular_baseline_submit/log_%j.err

set -euo pipefail
export SLURM_EXPORT_ENV=ALL

WORK_DIR="${WORK_DIR:-$HOME/veremi_collusion}"
BASELINE_JOB_SCRIPT="${BASELINE_JOB_SCRIPT:-$WORK_DIR/scripts/train_eval_datareplay_tabular_baselines.slurm}"
EVAL_SPLIT="${EVAL_SPLIT:-test}"   # val / test

DATE_TAG="$(date +%Y%m%d_%H%M%S)"

CONFIG_DATAREPLAY="${CONFIG_DATAREPLAY:-$HOME/veremi_collusion_local/qwen2.5_7b_datareplay_plausibility_recentk_autoq.yaml}"
CONFIG_DOS="${CONFIG_DOS:-$HOME/veremi_collusion_local/qwen2.5_7b_dosdisruptive_plausibility_recentk_autoq_classic.yaml}"
CONFIG_GRIDSYBIL="${CONFIG_GRIDSYBIL:-$HOME/veremi_collusion_local/qwen2.5_7b_gridsybil_plausibility_recentk_autoq_t1.yaml}"

if [[ ! -f "$BASELINE_JOB_SCRIPT" ]]; then
  echo "[ERROR] BASELINE_JOB_SCRIPT not found: $BASELINE_JOB_SCRIPT" >&2
  exit 2
fi

cd "$WORK_DIR"

echo "WORK_DIR=$WORK_DIR"
echo "BASELINE_JOB_SCRIPT=$BASELINE_JOB_SCRIPT"
echo "EVAL_SPLIT=$EVAL_SPLIT"
echo "DATE_TAG=$DATE_TAG"
echo "CONFIG_DATAREPLAY=$CONFIG_DATAREPLAY"
echo "CONFIG_DOS=$CONFIG_DOS"
echo "CONFIG_GRIDSYBIL=$CONFIG_GRIDSYBIL"

submit_one() {
  local attack="$1"
  local model_group="$2"
  local cfg="$3"

  local run_id="${DATE_TAG}_${attack}_${model_group}"
  local output_json="/scratch/$USER/veremi_collusion/outputs/tabular_baselines/${run_id}.json"

  local submit_out
  submit_out=$(sbatch --export=ALL,WORK_DIR="$WORK_DIR",CONFIG="$cfg",MODEL="$model_group",EVAL_SPLIT="$EVAL_SPLIT",RUN_ID="$run_id",OUTPUT_JSON="$output_json" "$BASELINE_JOB_SCRIPT")
  local job_id
  job_id="$(echo "$submit_out" | awk '{print $4}')"

  echo "[submit] attack=$attack model_group=$model_group job_id=$job_id"
  echo "         run_id=$run_id"
  echo "         output_json=$output_json"
}

# 3 attack types x 2 model groups = 6 jobs
submit_one "datareplay" "all_wo_svm" "$CONFIG_DATAREPLAY"
submit_one "datareplay" "svm" "$CONFIG_DATAREPLAY"

submit_one "dosdisruptive" "all_wo_svm" "$CONFIG_DOS"
submit_one "dosdisruptive" "svm" "$CONFIG_DOS"

submit_one "gridsybil" "all_wo_svm" "$CONFIG_GRIDSYBIL"
submit_one "gridsybil" "svm" "$CONFIG_GRIDSYBIL"

echo "[done] all tabular baseline jobs submitted."
