#!/bin/bash
#SBATCH --job-name=para-size-experiment
#SBATCH --partition=work1
#SBATCH --time=00:30:00
#SBATCH --mem=2gb
#SBATCH --cpus-per-task=1
#SBATCH --output=/scratch/%u/veremi_collusion/slurm/para_size_experiment/log_%j.out
#SBATCH --error=/scratch/%u/veremi_collusion/slurm/para_size_experiment/log_%j.err

set -euo pipefail
export SLURM_EXPORT_ENV=ALL

WORK_DIR="${WORK_DIR:-$HOME/veremi_collusion}"
cd "$WORK_DIR"

# 如果你希望每个实验都串行（一个接一个），保持 true
SERIAL="${SERIAL:-true}"

# 你可以改顺序；这里按 gridsybil -> dos -> replay，且 0.5b -> 1.5b -> 3b
EXPERIMENTS=(
  "gridsybil_0.5b|scripts/train_gridsybil_plausibility_recentk_autoq_t1_0.5b.slurm|scripts/eval_gridsybil_plausibility_recentk_autoq_t1_0.5b.slurm|gridsybil_recentk_autoq_t1_0.5b"
  "gridsybil_1.5b|scripts/train_gridsybil_plausibility_recentk_autoq_t1_1.5b.slurm|scripts/eval_gridsybil_plausibility_recentk_autoq_t1_1.5b.slurm|gridsybil_recentk_autoq_t1_1.5b"
  "gridsybil_3b|scripts/train_gridsybil_plausibility_recentk_autoq_t1_3b.slurm|scripts/eval_gridsybil_plausibility_recentk_autoq_t1_3b.slurm|gridsybil_recentk_autoq_t1_3b"

  "dos_0.5b|scripts/train_dosdisruptive_plausibility_recentk_autoq_classic_0.5b.slurm|scripts/eval_dosdisruptive_plausibility_recentk_autoq_classic_0.5b.slurm|dosdisruptive_autoq_classic_0.5b"
  "dos_1.5b|scripts/train_dosdisruptive_plausibility_recentk_autoq_classic_1.5b.slurm|scripts/eval_dosdisruptive_plausibility_recentk_autoq_classic_1.5b.slurm|dosdisruptive_autoq_classic_1.5b"
  "dos_3b|scripts/train_dosdisruptive_plausibility_recentk_autoq_classic_3b.slurm|scripts/eval_dosdisruptive_plausibility_recentk_autoq_classic_3b.slurm|dosdisruptive_autoq_classic_3b"

  "replay_0.5b|scripts/train_datareplay_plausibility_recentk_autoq_t1_0.5b.slurm|scripts/eval_datareplay_plausibility_recentk_autoq_t1_0.5b.slurm|datareplay_recentk_autoq_0.5b"
  "replay_1.5b|scripts/train_datareplay_plausibility_recentk_autoq_t1_1.5b.slurm|scripts/eval_datareplay_plausibility_recentk_autoq_t1_1.5b.slurm|datareplay_recentk_autoq_1.5b"
  "replay_3b|scripts/train_datareplay_plausibility_recentk_autoq_t1_3b.slurm|scripts/eval_datareplay_plausibility_recentk_autoq_t1_3b.slurm|datareplay_recentk_autoq_3b"
)

extract_job_id() {
  # sbatch 输出通常是: Submitted batch job 12345678
  awk '{print $4}'
}

prev_job=""
for item in "${EXPERIMENTS[@]}"; do
  IFS='|' read -r tag train_script eval_script exp_name <<< "$item"

  [[ -f "$train_script" ]] || { echo "[ERROR] missing $train_script"; exit 2; }
  [[ -f "$eval_script"  ]] || { echo "[ERROR] missing $eval_script"; exit 2; }

  echo "==== $tag ===="

  # 训练提交：若串行且有上一个任务，则依赖上一个 eval 成功后再开始
  if [[ "$SERIAL" == "true" && -n "$prev_job" ]]; then
    train_submit=$(sbatch --dependency=afterok:${prev_job} "$train_script")
  else
    train_submit=$(sbatch "$train_script")
  fi
  train_job=$(echo "$train_submit" | extract_job_id)
  echo "train: $train_submit"

  # 评估提交：依赖当前训练成功
  eval_submit=$(sbatch --dependency=afterok:${train_job} --export=ALL,EXP_NAME=${exp_name} "$eval_script")
  eval_job=$(echo "$eval_submit" | extract_job_id)
  echo "eval : $eval_submit"

  # 下一个实验若串行，依赖当前 eval
  prev_job="$eval_job"
done

echo "All submissions done."