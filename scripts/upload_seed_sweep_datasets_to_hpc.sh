#!/bin/bash
# Upload the 30 seed-sweep sampled datasets (3 attacks x 10 seeds) from local
# data/processed/ to HPC scratch, matching the paths that scripts/seed_sweep/*.slurm
# expect via PARQUET_DIR.
#
# Run this from the repo root, from a machine that can actually reach the HPC login
# node (this sandbox got "Connection refused" reaching slogin.palmetto.clemson.edu --
# likely needs campus VPN and/or interactive Duo 2FA, so run this yourself).
#
# Usage:
#   ./scripts/upload_seed_sweep_datasets_to_hpc.sh [ssh_host]
#
# ssh_host defaults to "palmetto" (matches the Host alias in ~/.ssh/config).

set -euo pipefail

SSH_HOST="${1:-palmetto}"
SEEDS=(2 18 25 37 45 57 65 75 80 98)

# local_dir_pattern (printf with seed) -> remote_dir_pattern (printf with seed)
declare -A LOCAL_PATTERN=(
  [gridsybil]="data/processed/plausibility_messages_senderk_autoq_mid_v2_split_sampled_50k_20k_50k_seed%s"
  [datareplay]="data/processed/datareplay_messages_senderk_autoq_t1_split_sampled_50k_20k_50k_seed%s"
  [dosdisruptive]="data/processed/dosdisruptive_senderk_autoq_classic_split_sampled_50k_20k_50k_seed%s"
)

declare -A REMOTE_PATTERN=(
  [gridsybil]="veremi_collusion/data/gridsybil_plausibility_senderk_50k-seed%s"
  [datareplay]="veremi_collusion/data/datareplay_plausibility_senderk_autoq_50k-seed%s"
  [dosdisruptive]="veremi_collusion/data/dosdisruptive_plausibility_senderk_autoq_classic_50k-seed%s"
)

echo "[check] testing SSH connectivity to $SSH_HOST ..."
if ! ssh -o BatchMode=yes -o ConnectTimeout=8 "$SSH_HOST" "echo ok" >/dev/null 2>&1; then
  echo "[ERROR] Cannot reach $SSH_HOST non-interactively." >&2
  echo "        If this requires VPN / Duo 2FA, connect/authenticate first, then rerun." >&2
  exit 1
fi

REMOTE_SCRATCH_ROOT="/scratch/$(ssh "$SSH_HOST" 'echo $USER')"
echo "[info] remote scratch root: $REMOTE_SCRATCH_ROOT"

for attack in gridsybil datareplay dosdisruptive; do
  for seed in "${SEEDS[@]}"; do
    local_dir=$(printf "${LOCAL_PATTERN[$attack]}" "$seed")
    remote_dir="$REMOTE_SCRATCH_ROOT/$(printf "${REMOTE_PATTERN[$attack]}" "$seed")"

    if [[ ! -d "$local_dir" ]]; then
      echo "[ERROR] Missing local dir: $local_dir" >&2
      exit 2
    fi

    echo "=== attack=$attack seed=$seed ==="
    echo "  local:  $local_dir"
    echo "  remote: $remote_dir"

    ssh "$SSH_HOST" "mkdir -p '$remote_dir'"
    # Trailing "/." on the source copies contents into remote_dir rather than
    # nesting another directory level named after the local dir.
    scp -r "$local_dir/." "$SSH_HOST:$remote_dir/"
  done
done

echo "[done] uploaded 30 seed-sweep dataset directories to $SSH_HOST:$REMOTE_SCRATCH_ROOT"
