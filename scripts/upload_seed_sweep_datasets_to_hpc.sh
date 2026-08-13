#!/bin/bash
# Upload the 30 seed-sweep sampled datasets (3 attacks x 10 seeds) from local
# data/processed/ to HPC scratch, matching the paths that scripts/seed_sweep/*.slurm
# expect via PARQUET_DIR.
#
# Remote directory names mirror the local basenames verbatim (data/processed/<X> ->
# /scratch/$USER/veremi_collusion/data/<X>) -- no renaming. This intentionally does NOT
# match the shorter seed=42-style names used by the checked-in YAML configs' default
# parquet_dir; the seed-sweep scripts override parquet_dir explicitly so that's fine.
#
# Run this from the repo root, from a machine that can actually reach the HPC login
# node (this sandbox got "Connection refused" reaching slogin.palmetto.clemson.edu --
# likely needs campus VPN and/or interactive Duo 2FA, so run this yourself).
#
# Usage:
#   ./scripts/upload_seed_sweep_datasets_to_hpc.sh [ssh_host]
#
# ssh_host defaults to "palmetto" (matches the Host alias in ~/.ssh/config).
# Already-uploaded seeds (remote dir already has train.parquet) are skipped.

set -euo pipefail

SSH_HOST="${1:-palmetto}"
SEEDS=(2 18 25 37 45 57 65 75 80 98)

LOCAL_DIR_BASENAME=(
  "plausibility_messages_senderk_autoq_mid_v2_split_sampled_50k_20k_50k_seed%s"   # gridsybil
  "datareplay_messages_senderk_autoq_t1_split_sampled_50k_20k_50k_seed%s"         # datareplay
  "dosdisruptive_senderk_autoq_classic_split_sampled_50k_20k_50k_seed%s"          # dosdisruptive
)

echo "[check] testing SSH connectivity to $SSH_HOST ..."
if ! ssh -o BatchMode=yes -o ConnectTimeout=8 "$SSH_HOST" "echo ok" >/dev/null 2>&1; then
  echo "[ERROR] Cannot reach $SSH_HOST non-interactively." >&2
  echo "        If this requires VPN / Duo 2FA, connect/authenticate first, then rerun." >&2
  exit 1
fi

REMOTE_SCRATCH_ROOT="/scratch/$(ssh "$SSH_HOST" 'echo $USER')/veremi_collusion/data"
echo "[info] remote data root: $REMOTE_SCRATCH_ROOT"

for pattern in "${LOCAL_DIR_BASENAME[@]}"; do
  for seed in "${SEEDS[@]}"; do
    basename=$(printf "$pattern" "$seed")
    local_dir="data/processed/$basename"
    remote_dir="$REMOTE_SCRATCH_ROOT/$basename"

    if [[ ! -d "$local_dir" ]]; then
      echo "[ERROR] Missing local dir: $local_dir" >&2
      exit 2
    fi

    if ssh "$SSH_HOST" "test -f '$remote_dir/train.parquet'"; then
      echo "[skip] already uploaded: $remote_dir"
      continue
    fi

    echo "=== $basename ==="
    echo "  local:  $local_dir"
    echo "  remote: $remote_dir"

    ssh "$SSH_HOST" "mkdir -p '$remote_dir'"
    scp -r "$local_dir/." "$SSH_HOST:$remote_dir/"
  done
done

echo "[done] seed-sweep dataset directories present under $SSH_HOST:$REMOTE_SCRATCH_ROOT"
