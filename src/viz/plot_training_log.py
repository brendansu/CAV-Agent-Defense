from __future__ import annotations

"""
Utility script to parse Phase 1 training Slurm logs and plot loss curves.

It expects logs produced by `src.training.train_lora_qwen_phase1_binary`,
which contain:
- A line like:
    Estimated steps_per_epoch=6250, total_training_steps=12500, warmup_steps=375
- Periodic training logs:
    {'loss': '0.7691', 'grad_norm': '0.3343', 'learning_rate': '7.973e-05', 'epoch': '0.048'}
- Periodic eval logs:
    {'eval_loss': '0.3652', 'eval_runtime': '62.65', 'eval_samples_per_second': '15.96', 'eval_steps_per_second': '3.991', 'epoch': '0.08'}

Usage (from repo root, example):

    python -m src.viz.plot_training_log \\
        --log slurm/train_debug_ep2.0_job10270943.out \\
        --output slurm/train_debug_ep2.0_job10270943_loss.png

By default it plots all points (no subsampling).
"""

import argparse
import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List

import matplotlib.pyplot as plt


@dataclass
class TrainingCurves:
    steps: List[float]
    losses: List[float]
    grad_norms: List[float]
    learning_rates: List[float]
    eval_steps: List[float]
    eval_losses: List[float]
    steps_per_epoch: float


_STEPS_PER_EPOCH_RE = re.compile(
    r"Estimated steps_per_epoch=(\d+), total_training_steps=(\d+), warmup_steps=(\d+)"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Parse a Phase 1 Slurm training log and plot train/eval loss vs. step. "
            "Assumes logs from src.training.train_lora_qwen_phase1_binary."
        )
    )
    parser.add_argument(
        "--log",
        type=str,
        required=True,
        help="Path to Slurm output log (e.g. slurm/train_debug_ep2.0_job10270943.out).",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help=(
            "Path to output PNG. "
            "If not provided, defaults to <log_stem>_loss.png in the same directory."
        ),
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="If set, also show the plot window (useful when running locally).",
    )
    parser.add_argument(
        "--train-ymin",
        type=float,
        default=None,
        help="Optional lower bound for the train loss axis. If omitted, chosen automatically.",
    )
    parser.add_argument(
        "--train-ymax",
        type=float,
        default=None,
        help="Optional upper bound for the train loss axis. If omitted, chosen automatically.",
    )
    parser.add_argument(
        "--eval-ymin",
        type=float,
        default=None,
        help="Optional lower bound for the eval loss axis. If omitted, chosen automatically.",
    )
    parser.add_argument(
        "--eval-ymax",
        type=float,
        default=None,
        help="Optional upper bound for the eval loss axis. If omitted, chosen automatically.",
    )
    return parser.parse_args()


def _infer_steps_per_epoch(lines: List[str]) -> float:
    """
    Find the 'Estimated steps_per_epoch=...' line and return steps_per_epoch.
    """
    for line in lines:
        m = _STEPS_PER_EPOCH_RE.search(line)
        if m:
            steps_per_epoch_str = m.group(1)
            return float(steps_per_epoch_str)
    raise ValueError(
        "Could not find 'Estimated steps_per_epoch=...' line in the log; "
        "cannot infer step indices."
    )


def parse_training_log(log_path: Path) -> TrainingCurves:
    """
    Parse the Slurm training log and extract train/eval curves.
    """
    if not log_path.exists():
        raise FileNotFoundError(f"Log file not found: {log_path}")

    with log_path.open("r", encoding="utf-8") as f:
        lines = f.readlines()

    steps_per_epoch = _infer_steps_per_epoch(lines)

    steps: List[float] = []
    losses: List[float] = []
    grad_norms: List[float] = []
    learning_rates: List[float] = []

    eval_steps: List[float] = []
    eval_losses: List[float] = []

    for line in lines:
        stripped = line.strip()
        # Fast path: most training/eval logs are pure dict literals on the line.
        if not (stripped.startswith("{") and stripped.endswith("}")):
            continue

        try:
            record = ast.literal_eval(stripped)
        except (SyntaxError, ValueError):
            # Skip anything that is not a simple Python dict literal.
            continue

        if not isinstance(record, dict):
            continue

        epoch_raw = record.get("epoch")
        if epoch_raw is None:
            continue
        try:
            epoch = float(epoch_raw)
        except (TypeError, ValueError):
            continue

        step = epoch * steps_per_epoch

        if "loss" in record:
            # Training step log
            try:
                loss_val = float(record["loss"])
            except (TypeError, ValueError):
                loss_val = float("nan")

            grad_raw = record.get("grad_norm")
            lr_raw = record.get("learning_rate")

            try:
                grad_val = float(grad_raw) if grad_raw is not None else float("nan")
            except (TypeError, ValueError):
                grad_val = float("nan")

            try:
                lr_val = float(lr_raw) if lr_raw is not None else float("nan")
            except (TypeError, ValueError):
                lr_val = float("nan")

            steps.append(step)
            losses.append(loss_val)
            grad_norms.append(grad_val)
            learning_rates.append(lr_val)

        elif "eval_loss" in record:
            # Periodic eval log
            try:
                eval_loss_val = float(record["eval_loss"])
            except (TypeError, ValueError):
                eval_loss_val = float("nan")

            eval_steps.append(step)
            eval_losses.append(eval_loss_val)

    if not steps:
        raise ValueError(f"No training loss records found in log: {log_path}")

    return TrainingCurves(
        steps=steps,
        losses=losses,
        grad_norms=grad_norms,
        learning_rates=learning_rates,
        eval_steps=eval_steps,
        eval_losses=eval_losses,
        steps_per_epoch=steps_per_epoch,
    )


def plot_loss_curves(
    curves: TrainingCurves,
    output_path: Path,
    title: str | None = None,
    show: bool = False,
    train_ymin: float | None = None,
    train_ymax: float | None = None,
    eval_ymin: float | None = None,
    eval_ymax: float | None = None,
) -> None:
    """
    Plot train/eval loss vs step and save to output_path.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fig, ax1 = plt.subplots(figsize=(8, 5))

    # Left y-axis: training loss
    ax1.plot(
        curves.steps,
        curves.losses,
        label="train loss",
        color="C0",
        linewidth=1.2,
        alpha=0.9,
    )

    ax2 = None
    if curves.eval_steps and curves.eval_losses:
        # Right y-axis: eval loss (typically on a different scale)
        ax2 = ax1.twinx()
        ax2.plot(
            curves.eval_steps,
            curves.eval_losses,
            label="eval loss",
            color="C1",
            alpha=0.9,
        )

    ax1.set_xlabel("Step")
    ax1.set_ylabel("Train loss")
    if ax2 is not None:
        ax2.set_ylabel("Eval loss")

    # Manual y-limits for train loss (if provided)
    if train_ymin is not None or train_ymax is not None:
        cur_ymin, cur_ymax = ax1.get_ylim()
        if train_ymin is None:
            train_ymin = cur_ymin
        if train_ymax is None:
            train_ymax = cur_ymax
        ax1.set_ylim(train_ymin, train_ymax)

    # Manual y-limits for eval loss (if provided and axis exists)
    if ax2 is not None and (eval_ymin is not None or eval_ymax is not None):
        cur_ymin, cur_ymax = ax2.get_ylim()
        if eval_ymin is None:
            eval_ymin = cur_ymin
        if eval_ymax is None:
            eval_ymax = cur_ymax
        ax2.set_ylim(eval_ymin, eval_ymax)

    if title:
        ax1.set_title(title)

    ax1.grid(True, linestyle="--", linewidth=0.5, alpha=0.5)

    # Combine legends from both axes (if eval present)
    handles1, labels1 = ax1.get_legend_handles_labels()
    if ax2 is not None:
        handles2, labels2 = ax2.get_legend_handles_labels()
        handles = handles1 + handles2
        labels = labels1 + labels2
    else:
        handles, labels = handles1, labels1
    if handles:
        ax1.legend(handles, labels, loc="best")

    fig.tight_layout()
    fig.savefig(output_path, dpi=150)

    if show:
        plt.show()
    plt.close(fig)


def main() -> None:
    args = parse_args()
    log_path = Path(args.log)

    curves = parse_training_log(log_path)

    if args.output is not None:
        output_path = Path(args.output)
    else:
        # Default: same directory, "<stem>_loss.png"
        output_path = log_path.with_name(f"{log_path.stem}_loss.png")

    print(f"Log file:      {log_path}")
    print(f"Steps/epoch:   {curves.steps_per_epoch}")
    print(f"# train points: {len(curves.steps)}")
    print(f"# eval points:  {len(curves.eval_steps)}")
    print(f"Output figure: {output_path}")

    title = f"Training/eval loss vs step (steps_per_epoch={curves.steps_per_epoch:g})"
    plot_loss_curves(
        curves,
        output_path=output_path,
        title=title,
        show=args.show,
        train_ymin=args.train_ymin,
        train_ymax=args.train_ymax,
        eval_ymin=args.eval_ymin,
        eval_ymax=args.eval_ymax,
    )


if __name__ == "__main__":
    main()
