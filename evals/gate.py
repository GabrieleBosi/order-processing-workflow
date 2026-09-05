"""The acceptance gate. Reads a logged run, compares it to thresholds, exits 1 below any.

    python -m evals.gate                  # gate the run in evals/last_run.json
    python -m evals.gate --run-id <id>    # gate a specific MLflow run

The gate is deliberately dumb, the way mlops-loop's is. It does not run the
suite, it does not choose which numbers to look at, and it cannot pass by
being clever: it reads thresholds from a committed file, reads metrics from a
logged run, and compares. Anything it cannot compute is a failure, never a
silent skip - the one exception being the judge block, which stays switched
off by design until `judge_kappa` has been logged on the run.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import tracking

THRESHOLDS_PATH = tracking.REPO_ROOT / "evals" / "thresholds.yaml"
LAST_RUN_PATH = tracking.REPO_ROOT / "evals" / "last_run.json"

HIGHER = "higher_is_better"
LOWER = "lower_is_better"


class GateError(Exception):
    """The gate could not be evaluated at all. Never a silent pass."""


@dataclass(frozen=True)
class Check:
    metric: str
    direction: str
    threshold: float
    value: float
    comment: str = ""

    @property
    def passed(self) -> bool:
        return self.value >= self.threshold if self.direction == HIGHER else self.value <= self.threshold

    @property
    def margin(self) -> float:
        return self.value - self.threshold if self.direction == HIGHER else self.threshold - self.value


def load_thresholds(path: Path | None = None) -> dict:
    path = path or THRESHOLDS_PATH
    if not path.is_file():
        raise GateError(f"no thresholds file at {path}")
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not cfg.get("metrics"):
        raise GateError(f"{path} declares no metrics")
    for metric, spec in cfg["metrics"].items():
        if spec.get("direction") not in (HIGHER, LOWER):
            raise GateError(
                f"threshold {metric!r} has direction {spec.get('direction')!r}; "
                f"expected {HIGHER!r} or {LOWER!r}"
            )
        if not isinstance(spec.get("threshold"), (int, float)):
            raise GateError(f"threshold {metric!r} has no numeric threshold")
    return cfg


def load_metrics(run_id: str | None) -> tuple[str, dict[str, float]]:
    """Metrics for the run under test, straight from the tracking store."""
    if run_id is None:
        if not LAST_RUN_PATH.is_file():
            raise GateError(
                f"no {LAST_RUN_PATH} and no --run-id: run `python -m evals.run_mlflow` first"
            )
        run_id = json.loads(LAST_RUN_PATH.read_text(encoding="utf-8"))["run_id"]

    import mlflow

    tracking.configure()
    try:
        run = mlflow.get_run(run_id)
    except Exception as exc:  # noqa: BLE001 - any failure here is a gate failure
        raise GateError(f"cannot read MLflow run {run_id}: {exc}") from exc
    return run_id, dict(run.data.metrics)


def evaluate(cfg: dict, metrics: dict[str, float]) -> tuple[list[Check], list[str]]:
    """Build one Check per threshold line. Returns (checks, notes)."""
    checks: list[Check] = []
    notes: list[str] = []

    judge_cfg = cfg.get("judge") or {}
    kappa_metric = judge_cfg.get("requires_metric", "judge_kappa")
    judge_enabled = kappa_metric in metrics
    if judge_cfg and not judge_enabled:
        notes.append(
            f"judge thresholds NOT enforced: {kappa_metric} has never been logged on this run. "
            "Fill evals/labels.jsonl and run `python -m evals.calibrate_judge`."
        )

    for metric, spec in cfg["metrics"].items():
        if spec.get("judge_gated") and not judge_enabled:
            continue
        if metric not in metrics:
            raise GateError(
                f"metric {metric!r} is in {THRESHOLDS_PATH.name} but not in the run. "
                "The gate fails closed rather than skipping a line it cannot evaluate."
            )
        checks.append(Check(
            metric=metric,
            direction=spec["direction"],
            threshold=float(spec["threshold"]),
            value=float(metrics[metric]),
            comment=spec.get("why", ""),
        ))

    if judge_enabled and judge_cfg.get("min_kappa") is not None:
        checks.append(Check(
            metric=kappa_metric,
            direction=HIGHER,
            threshold=float(judge_cfg["min_kappa"]),
            value=float(metrics[kappa_metric]),
            comment="agreement with hand labels; below this the judge is not measuring anything",
        ))
    return checks, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.gate", description=__doc__)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--thresholds", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        cfg = load_thresholds(args.thresholds)
        run_id, metrics = load_metrics(args.run_id)
        checks, notes = evaluate(cfg, metrics)
    except GateError as exc:
        print(f"GATE ERROR: {exc}")
        return 1

    print(f"Gate on MLflow run {run_id}\n")
    width = max(len(c.metric) for c in checks)
    for check in checks:
        mark = "ok  " if check.passed else "FAIL"
        arrow = ">=" if check.direction == HIGHER else "<="
        print(f"  {mark} {check.metric:<{width}}  {check.value:9.4f} {arrow} "
              f"{check.threshold:<9.4f}  margin {check.margin:+.4f}")
    for note in notes:
        print(f"\n  [i] {note}")

    failures = [c for c in checks if not c.passed]
    print()
    if failures:
        print(f"GATE FAILED: {len(failures)} of {len(checks)} threshold(s) not met.")
        for check in failures:
            print(f"  - {check.metric}: {check.comment}" if check.comment else f"  - {check.metric}")
        return 1
    print(f"GATE PASSED: {len(checks)} threshold(s) met.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
