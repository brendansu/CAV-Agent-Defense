from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional, List, Dict

import pandas as pd


# 你的 run 目录名格式：
# AttackerTypeX-start=a,0.b-#c
RUN_DIR_RE = re.compile(
    r"^AttackerType(?P<attacker_type>\d+)-start=(?P<start_h>\d+),(?P<attacker_prob>0\.\d+)-#(?P<seed>\d+)$",
    re.IGNORECASE,
)

# 车辆文件名格式（你之前的 extract 统计也用到了）
VEH_RE = re.compile(r"^JSONlog-(?P<idx>\d+)-(?P<veh_id>\d+)-A(?P<is_attacker>[01])\.json$", re.IGNORECASE)


@dataclass
class RunManifestRow:
    run_id: str
    run_dir: str
    sca_path: Optional[str]
    groundtruth_path: Optional[str]
    vehicles_dir: str

    attacker_type: Optional[int]
    start_h: Optional[int]
    attacker_prob: Optional[float]
    seed: Optional[int]

    vehicle_files: int
    attacker_vehicle_files: int

    status: str
    note: Optional[str] = None


def parse_run_dirname(name: str) -> Dict[str, Optional[object]]:
    m = RUN_DIR_RE.match(name)
    if not m:
        return {
            "attacker_type": None,
            "start_h": None,
            "attacker_prob": None,
            "seed": None,
        }
    return {
        "attacker_type": int(m.group("attacker_type")),
        "start_h": int(m.group("start_h")),
        "attacker_prob": float(m.group("attacker_prob")),
        "seed": int(m.group("seed")),
    }


def scan_one_run(run_dir: Path) -> RunManifestRow:
    run_id = run_dir.name
    parts = parse_run_dirname(run_id)

    sca_dir = run_dir / "sca"
    gt_dir = run_dir / "groundtruth"
    veh_dir = run_dir / "vehicles"

    sca_files = list(sca_dir.glob("*.sca")) if sca_dir.exists() else []
    gt_files = list(gt_dir.glob("*.json")) if gt_dir.exists() else []
    veh_files = list(veh_dir.glob("JSONlog-*.json")) if veh_dir.exists() else []

    sca_path = str(sca_files[0]) if len(sca_files) == 1 else (str(sca_files[0]) if sca_files else None)
    groundtruth_path = str(gt_files[0]) if len(gt_files) == 1 else (str(gt_files[0]) if gt_files else None)

    attacker_vehicle_files = 0
    for p in veh_files:
        m = VEH_RE.match(p.name)
        if m and m.group("is_attacker") == "1":
            attacker_vehicle_files += 1

    # 健康检查
    status = "ok"
    notes = []
    if not veh_dir.exists():
        status = "error"
        notes.append("missing vehicles/ directory")
    if len(sca_files) != 1:
        status = "warn" if status == "ok" else status
        notes.append(f"sca_files={len(sca_files)} (expected 1)")
    if len(gt_files) != 1:
        status = "warn" if status == "ok" else status
        notes.append(f"groundtruth_files={len(gt_files)} (expected 1)")
    if len(veh_files) == 0:
        status = "error"
        notes.append("no vehicle JSON logs found")

    return RunManifestRow(
        run_id=run_id,
        run_dir=str(run_dir.resolve()),
        sca_path=sca_path,
        groundtruth_path=groundtruth_path,
        vehicles_dir=str(veh_dir.resolve()),
        attacker_type=parts["attacker_type"],
        start_h=parts["start_h"],
        attacker_prob=parts["attacker_prob"],
        seed=parts["seed"],
        vehicle_files=len(veh_files),
        attacker_vehicle_files=attacker_vehicle_files,
        status=status,
        note="; ".join(notes) if notes else None,
    )


def build_manifest(extracted_dir: Path) -> pd.DataFrame:
    run_dirs = sorted([p for p in extracted_dir.iterdir() if p.is_dir()])
    rows: List[dict] = []
    for rd in run_dirs:
        row = scan_one_run(rd)
        rows.append(asdict(row))
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--extracted_dir", type=str, required=True, help="e.g., data/extracted")
    ap.add_argument("--out_dir", type=str, default="data/processed/manifests")
    ap.add_argument("--write_csv", action="store_true")
    args = ap.parse_args()

    extracted_dir = Path(args.extracted_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    run_dirs = sorted([p for p in extracted_dir.iterdir() if p.is_dir()])
    print(f"Scanning {len(run_dirs)} run directories in {extracted_dir}", flush=True)

    df = build_manifest(extracted_dir)
    print(f"Built manifest: {len(df)} runs", flush=True)

    manifest_path = out_dir / "runs.parquet"
    df.to_parquet(manifest_path, index=False)

    # 可选：也写一个 csv，便于快速查看
    if args.write_csv:
        df.to_csv(out_dir / "runs.csv", index=False)

    # 可选：简单统计写到 json（numpy 类型需转为原生 Python 以便 JSON 序列化）
    def _to_native(obj):
        if hasattr(obj, "item"):
            val = obj.item()
            if isinstance(val, float) and pd.isna(val):
                return None
            return int(val) if isinstance(val, (int, bool)) or (isinstance(val, float) and val == int(val)) else val
        return obj

    summary = {
        "total_runs": int(len(df)),
        "status_counts": {str(k): _to_native(v) for k, v in df["status"].value_counts().items()},
        "attacker_type_counts": {_to_native(k): _to_native(v) for k, v in df["attacker_type"].value_counts(dropna=False).items()},
        "attacker_prob_counts": {_to_native(k): _to_native(v) for k, v in df["attacker_prob"].value_counts(dropna=False).items()},
        "start_h_counts": {_to_native(k): _to_native(v) for k, v in df["start_h"].value_counts(dropna=False).items()},
    }
    (out_dir / "runs_summary.json").write_text(json.dumps(summary, indent=2))

    print(f"Manifest written: {manifest_path}", flush=True)
    print(f"Summary written:  {out_dir / 'runs_summary.json'}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Error: {e}", flush=True)
        raise