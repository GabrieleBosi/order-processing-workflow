"""Calibrate the LLM judge against hand labels and log Cohen's kappa.

    python -m evals.make_labels_template     # 1. write the 20-row template
    # 2. fill in "human_score" (1-5) in evals/labels.jsonl, by hand,
    #    using evals/rubrics/extraction_fidelity.md
    python -m evals.run_mlflow --judge       # 3. get judge scores into a run
    python -m evals.calibrate_judge          # 4. kappa, logged onto that run

Field-guide rule 11: LLM-as-judge only for subjective quality, and calibrated
against human labels first. Until this script has logged `judge_kappa` on a
run, `evals/thresholds.yaml` keeps the judge out of the gate.

Three numbers are reported, and all three are logged:

  judge_kappa             Cohen's kappa, unweighted. The strict one: only an
                          exact score match counts as agreement. This is the
                          number the gate will eventually read.
  judge_kappa_quadratic   Quadratically weighted kappa. The scale is ordinal,
                          so a 4-vs-5 disagreement is not the same failure as
                          a 1-vs-5, and this one says so.
  judge_exact_agreement   Plain share of cases scored identically. Not chance
                          corrected; reported because kappa is unreadable
                          without it when the label distribution is skewed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import tracking
from .make_labels_template import LABELS_PATH, read_existing

MIN_LABELS = 20  # the brief asks for twenty; fewer is not a calibration


def load_judge_scores(report_path: Path) -> dict[str, int]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return {j["case"]: j["score"] for j in report.get("judge", [])}


def kappas(human: list[int], judge: list[int]) -> dict[str, float]:
    from sklearn.metrics import cohen_kappa_score

    exact = sum(int(a == b) for a, b in zip(human, judge, strict=True)) / len(human)
    out = {"judge_exact_agreement": exact}
    # Both raters constant on the same value: kappa is undefined (no variance
    # to correct for chance against). Say so rather than logging a fake 0.
    if len(set(human)) == 1 and len(set(judge)) == 1:
        out["judge_kappa"] = float("nan")
        out["judge_kappa_quadratic"] = float("nan")
        return out
    labels = sorted(set(human) | set(judge))
    out["judge_kappa"] = float(cohen_kappa_score(human, judge, labels=labels))
    out["judge_kappa_quadratic"] = float(
        cohen_kappa_score(human, judge, labels=labels, weights="quadratic")
    )
    return out


def interpret(kappa: float) -> str:
    """Landis & Koch bands, named so the number is readable without a lookup."""
    if kappa != kappa:  # nan
        return "undefined (both raters gave one constant score)"
    for bound, name in ((0.0, "poor"), (0.20, "slight"), (0.40, "fair"),
                        (0.60, "moderate"), (0.80, "substantial")):
        if kappa <= bound:
            return name
    return "almost perfect"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.calibrate_judge", description=__doc__)
    parser.add_argument(
        "--report", type=Path, default=tracking.REPO_ROOT / "evals" / "_artifacts" / "report.json",
        help="Harness report holding the judge scores (default: the last logged run's).",
    )
    parser.add_argument(
        "--run-id", default=None,
        help="MLflow run to log kappa onto (default: the run in evals/last_run.json).",
    )
    parser.add_argument("--min-labels", type=int, default=MIN_LABELS)
    parser.add_argument("--no-log", action="store_true", help="Compute but do not touch MLflow.")
    args = parser.parse_args(argv)

    rows = read_existing(LABELS_PATH)
    if not rows:
        print(f"No label file at {LABELS_PATH}. Run `python -m evals.make_labels_template` first.")
        return 1
    labelled = {c: r for c, r in rows.items() if r.get("human_score") is not None}
    if len(labelled) < args.min_labels:
        print(f"awaiting labels: {len(labelled)}/{args.min_labels} rows in {LABELS_PATH} "
              "have a human_score.")
        print("Fill them in by hand against evals/rubrics/extraction_fidelity.md, then re-run.")
        print("Nothing was logged; the judge stays out of the gate.")
        return 0

    if not args.report.is_file():
        print(f"No judge scores at {args.report}. Run `python -m evals.run_mlflow --judge` first.")
        return 1
    judge_scores = load_judge_scores(args.report)
    if not judge_scores:
        print(f"{args.report} holds no judge scores. Re-run the suite with --judge.")
        return 1

    paired = sorted(set(labelled) & set(judge_scores))
    missing = sorted(set(labelled) - set(judge_scores))
    if len(paired) < args.min_labels:
        print(f"Only {len(paired)} case(s) have both a hand label and a judge score "
              f"(missing judge scores for: {', '.join(missing) or 'none'}).")
        print("Nothing was logged; the judge stays out of the gate.")
        return 1

    human = [int(labelled[c]["human_score"]) for c in paired]
    judge = [int(judge_scores[c]) for c in paired]
    metrics = kappas(human, judge)
    metrics["judge_labelled_cases"] = float(len(paired))

    print(f"Judge calibration on {len(paired)} hand-labelled case(s)\n")
    print(f"{'case':<40} {'human':>5} {'judge':>5}  delta")
    for case, h, j in zip(paired, human, judge, strict=True):
        print(f"{case:<40} {h:>5} {j:>5}  {j - h:+d}")
    print()
    print(f"  Cohen's kappa (unweighted)   {metrics['judge_kappa']:.3f}   "
          f"({interpret(metrics['judge_kappa'])})")
    print(f"  Cohen's kappa (quadratic)    {metrics['judge_kappa_quadratic']:.3f}")
    print(f"  exact agreement              {metrics['judge_exact_agreement']:.3f}")

    if args.no_log:
        return 0

    import mlflow

    tracking.configure()
    run_id = args.run_id
    if run_id is None:
        pointer = tracking.REPO_ROOT / "evals" / "last_run.json"
        if not pointer.is_file():
            print(f"\nNo {pointer} and no --run-id: cannot decide which run to log onto.")
            return 1
        run_id = json.loads(pointer.read_text(encoding="utf-8"))["run_id"]
    with mlflow.start_run(run_id=run_id):
        mlflow.log_metrics({k: v for k, v in metrics.items() if v == v})  # drop nan
        mlflow.log_dict(
            {"cases": paired, "human": human, "judge": judge, **metrics},
            "judge_calibration.json",
        )
    print(f"\nLogged onto MLflow run {run_id}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
