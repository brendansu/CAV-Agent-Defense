from __future__ import annotations

import argparse
import json
import re
import tarfile
from dataclasses import dataclass
from pathlib import Path
import shutil
from typing import Optional, List, Tuple


SCA_RE = re.compile(r"AttackerType\d+-start=.*?\.sca$", re.IGNORECASE)
GT_RE = re.compile(r"GroundTruthJSONlog.*\.json$", re.IGNORECASE)
VEH_RE = re.compile(r"JSONlog-.*\.json$", re.IGNORECASE)

# 你的车辆文件名：JSONlog-a-b-Ac.json（用于统计 attacker_count / vehicle_count）
VEH_NAME_RE = re.compile(r"JSONlog-(\d+)-(\d+)-A(\d+)\.json$", re.IGNORECASE)


@dataclass
class RunFiles:
    sca_member: tarfile.TarInfo
    gt_member: tarfile.TarInfo
    vehicle_members: List[tarfile.TarInfo]


def _find_run_members(tar: tarfile.TarFile) -> RunFiles:
    """
    在 tgz 内部找：
      - 1 个 .sca
      - 1 个 GroundTruthJSONlog*.json
      - N 个 JSONlog-*.json
    并且尽量从同一个 results/ 目录下挑。
    """
    members = [m for m in tar.getmembers() if m.isfile()]

    sca = [m for m in members if SCA_RE.search(Path(m.name).name)]
    gt = [m for m in members if GT_RE.search(Path(m.name).name)]
    veh = [m for m in members if VEH_RE.search(Path(m.name).name)]

    if not sca:
        raise FileNotFoundError("No .sca file found in tgz")
    if not gt:
        raise FileNotFoundError("No GroundTruthJSONlog*.json found in tgz")
    if not veh:
        raise FileNotFoundError("No JSONlog-*.json vehicle logs found in tgz")

    # 如果有多个候选（极少数情况），优先选择路径里包含 results 的那个
    def prefer_results(ms: List[tarfile.TarInfo]) -> List[tarfile.TarInfo]:
        results_first = sorted(ms, key=lambda x: ("results" not in x.name.lower(), x.name))
        return results_first

    sca = prefer_results(sca)
    gt = prefer_results(gt)
    veh = prefer_results(veh)

    # 尽量保证三类文件来自同一个 results 目录前缀
    # 取 sca 的父目录作为 “run root”
    sca_dir = str(Path(sca[0].name).parent)
    gt_best = next((m for m in gt if str(Path(m.name).parent) == sca_dir), gt[0])
    veh_best = [m for m in veh if str(Path(m.name).parent) == sca_dir]
    if not veh_best:
        # 退化：如果 vehicle logs 不在 sca_dir，仍然全收
        veh_best = veh

    return RunFiles(sca_member=sca[0], gt_member=gt_best, vehicle_members=veh_best)


def _safe_extract_member(tar: tarfile.TarFile, member: tarfile.TarInfo, dst_path: Path) -> None:
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    with tar.extractfile(member) as f:
        if f is None:
            raise RuntimeError(f"Failed to extract file-like for member: {member.name}")
        with open(dst_path, "wb") as out:
            shutil.copyfileobj(f, out)


def _parse_vehicle_filename(fn: str) -> Optional[Tuple[int, int, int]]:
    m = VEH_NAME_RE.match(fn)
    if not m:
        return None
    a, b, c = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return a, b, c


def extract_one_tgz(tgz_path: Path, out_root: Path, overwrite: bool = False) -> dict:
    """
    抽取 tgz 中的 results 关键文件到 data/extracted/<run_id>/...
    返回 meta dict（后续可用于 manifest）。
    """
    tgz_path = tgz_path.resolve()
    if not tgz_path.exists():
        raise FileNotFoundError(tgz_path)

    with tarfile.open(tgz_path, "r:gz") as tar:
        rf = _find_run_members(tar)

        sca_name = Path(rf.sca_member.name).name
        run_id = Path(sca_name).stem  # 去掉 .sca
        run_dir = out_root / run_id

        if run_dir.exists():
            if not overwrite:
                return {
                    "run_id": run_id,
                    "tgz": str(tgz_path),
                    "status": "skipped_exists",
                }
            shutil.rmtree(run_dir)

        # 写文件
        _safe_extract_member(tar, rf.sca_member, run_dir / "sca" / sca_name)
        _safe_extract_member(tar, rf.gt_member, run_dir / "groundtruth" / Path(rf.gt_member.name).name)

        vehicle_count = 0
        attacker_count = 0
        for m in rf.vehicle_members:
            fn = Path(m.name).name
            parsed = _parse_vehicle_filename(fn)
            if parsed:
                vehicle_count += 1
                attacker_count += (1 if parsed[2] == 1 else 0)
            _safe_extract_member(tar, m, run_dir / "vehicles" / fn)

        meta = {
            "run_id": run_id,
            "tgz": str(tgz_path),
            "sca_file": sca_name,
            "groundtruth_file": Path(rf.gt_member.name).name,
            "vehicle_files": len(rf.vehicle_members),
            "vehicle_count_parsed": vehicle_count,
            "attacker_count_parsed": attacker_count,
            "status": "ok",
        }
        (run_dir / "meta.json").write_text(json.dumps(meta, indent=2))
        return meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw_dir", type=str, required=True, help="e.g., data/raw")
    ap.add_argument("--out_dir", type=str, required=True, help="e.g., data/extracted")
    ap.add_argument("--max_runs", type=int, default=0, help="0 = all")
    ap.add_argument("--overwrite", action="store_true")
    args = ap.parse_args()

    raw_dir = Path(args.raw_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    tgzs = sorted(list(raw_dir.glob("*.tgz")) + list(raw_dir.glob("*.tar.gz")))
    if args.max_runs and args.max_runs > 0:
        tgzs = tgzs[: args.max_runs]

    manifest = []
    for p in tgzs:
        try:
            meta = extract_one_tgz(p, out_dir, overwrite=args.overwrite)
        except Exception as e:
            meta = {"tgz": str(p), "status": "error", "error": repr(e)}
        manifest.append(meta)

    # 写一个简单 manifest（JSON）
    (out_dir / "manifest_extract.json").write_text(json.dumps(manifest, indent=2))
    print(f"Done. Extracted manifest written to: {out_dir / 'manifest_extract.json'}")


if __name__ == "__main__":
    main()