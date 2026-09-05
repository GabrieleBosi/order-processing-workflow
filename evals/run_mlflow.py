"""Run the whole eval suite and log exactly one MLflow run for it.

    python -m evals.run_mlflow                 # full suite, objective checks only
    python -m evals.run_mlflow --judge         # also score the 20 judge cases
    python -m evals.run_mlflow --dry-run       # cost estimate only, no API calls

What lands in MLflow, per execution:

    params    model id, judge model, temperature, git blob hash of each prompt
              file, suite version + content hash, git commit, llm mode
    metrics   pass rate overall and per category, mean and p95 case latency,
              total cost and cost per case, per-component check pass rates
    artifacts results.csv / results.md   - one row per case
              failures.csv / failures.md - the failing rows only
              report.json                - the raw harness report
    traces    one span per case (plus one per judge call), with the Anthropic
              SDK auto-instrumented underneath, so every case is inspectable

The tracking store is sqlite:///mlflow.db in the repository root and is
gitignored: runs are local evidence, not committed artifacts.

Budget guard (field-guide rule 21): a dry token estimate runs before the first
API call and the suite refuses to start if it exceeds --budget-usd.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import mlflow

from order_workflow.config import load_config
from order_workflow.evals import DEFAULT_CASES_DIR, run_evals
from order_workflow.llm import MODEL_PRICES, get_llm
from order_workflow.steps import extract as extract_step
from order_workflow.steps import normalize as normalize_step
from order_workflow.steps.check import CHECK_SYSTEM
from order_workflow.steps.extract import EXTRACTION_SYSTEM

from . import tracking

REPO_ROOT = tracking.REPO_ROOT
ARTIFACT_DIR = REPO_ROOT / "evals" / "_artifacts"
DEFAULT_BUDGET_USD = 1.00

# Rough but deliberately pessimistic: ~3.2 characters per token for the mixed
# Italian / French / German text in this suite, and generous output sizes. The
# estimate exists to stop a runaway bill, so it should over-count, never under.
CHARS_PER_TOKEN = 3.2


# --------------------------------------------------------------- estimate


def estimate_cost(config, cases_dir: Path, judge: bool) -> dict:
    """Token/cost estimate for one full suite run, without calling the API.

    Step 1 (normalize) is pure code and runs for real here, because whether a
    document has an order table is exactly what decides if the extraction step
    costs anything at all.
    """
    price_in, price_out = MODEL_PRICES.get(config.model, (5.0, 25.0))
    judge_in, judge_out = MODEL_PRICES.get(config.judge_model, (2.0, 10.0))

    calls = {"extract": 0, "check": 0, "judge": 0}
    tokens_in = tokens_out = 0.0
    judge_tokens_in = judge_tokens_out = 0.0

    for case_dir in sorted(d for d in cases_dir.iterdir() if (d / "case.json").exists()):
        spec = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
        doc = normalize_step.run(case_dir / spec["input"])
        rendered = extract_step._render_document(doc)
        n_lines = max(1, spec["expected"].get("n_lines", 1))

        has_table = any(extract_step.table_is_order_table(t) for t in doc.tables)
        if not has_table:
            # Step 2 makes TWO calls per document without an order table - the
            # header and the lines, same prompt, one output schema each,
            # because the combined schema is rejected as too complex (see
            # steps/extract.py). Both calls carry the whole document, so the
            # input is counted twice.
            calls["extract"] += 2
            tokens_in += 2 * (len(EXTRACTION_SYSTEM) + len(rendered)) / CHARS_PER_TOKEN
            tokens_out += 120 + (120 + 90 * n_lines)

        # Step 4 asks the model only about irregular lines. We cannot know
        # which before running, so the estimate assumes every line is one.
        calls["check"] += n_lines
        tokens_in += n_lines * (len(CHECK_SYSTEM) / CHARS_PER_TOKEN + 400)
        tokens_out += n_lines * 150

        if judge and "judge" in spec:
            calls["judge"] += 1
            judge_tokens_in += (len(rendered) + 600 * n_lines + 400) / CHARS_PER_TOKEN
            judge_tokens_out += 200

    pipeline_cost = tokens_in / 1e6 * price_in + tokens_out / 1e6 * price_out
    judge_cost = judge_tokens_in / 1e6 * judge_in + judge_tokens_out / 1e6 * judge_out
    return {
        "calls": calls,
        "estimated_input_tokens": int(tokens_in),
        "estimated_output_tokens": int(tokens_out),
        "estimated_pipeline_cost_usd": round(pipeline_cost, 4),
        "estimated_judge_cost_usd": round(judge_cost, 4),
        "estimated_total_cost_usd": round(pipeline_cost + judge_cost, 4),
    }


def print_estimate(est: dict, budget: float) -> None:
    calls = est["calls"]
    print("Dry cost estimate (no API calls made):")
    print(f"  extract calls   {calls['extract']:>4}")
    print(f"  check calls     {calls['check']:>4}   (upper bound: every line treated as irregular)")
    print(f"  judge calls     {calls['judge']:>4}")
    print(f"  input tokens  ~{est['estimated_input_tokens']:>7,}   "
          f"output tokens ~{est['estimated_output_tokens']:,}")
    print(f"  pipeline ${est['estimated_pipeline_cost_usd']:.4f} + "
          f"judge ${est['estimated_judge_cost_usd']:.4f} = "
          f"${est['estimated_total_cost_usd']:.4f}   (budget ${budget:.2f})")


# ------------------------------------------------------------- artifacts

RESULT_COLUMNS = [
    "case", "category", "status", "first_failing_component", "checks",
    "extraction_method", "order_verdict", "duration_ms", "cost_usd", "title", "error", "failed",
]


def _rows(results: list[dict]) -> list[dict]:
    rows = []
    for r in results:
        row = {c: r.get(c, "") for c in RESULT_COLUMNS}
        row["failed"] = " | ".join(r.get("failed", []))
        rows.append(row)
    return rows


def write_tables(results: list[dict], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = _rows(results)
    failures = [r for r in rows if r["status"] == "fail"]
    written = []
    for name, subset in (("results", rows), ("failures", failures)):
        csv_path = out_dir / f"{name}.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=RESULT_COLUMNS)
            writer.writeheader()
            writer.writerows(subset)
        md_path = out_dir / f"{name}.md"
        md_path.write_text(_markdown_table(name, subset), encoding="utf-8")
        written += [csv_path, md_path]
    return written


def _markdown_table(name: str, rows: list[dict]) -> str:
    cols = [c for c in RESULT_COLUMNS if c != "title"]
    head = f"# {name} ({len(rows)} rows)\n\n"
    if not rows:
        return head + "Nothing to report.\n"
    out = [head, "| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(r[c]).replace("|", "/") for c in cols) + " |")
    return "\n".join(out) + "\n"


# --------------------------------------------------------- instrumentation


@contextmanager
def instrument_llm_client():
    """Put every LLM call of the run inside an MLflow span, then undo it.

    `mlflow.anthropic.autolog()` patches `Messages.create`. This codebase calls
    `Messages.parse` for structured output, which posts directly and never goes
    through `create`, so autolog alone records nothing. Rather than reshape the
    client to suit the tracer - this session measures, it does not change the
    system under test - the eval runner wraps `LLMClient.structured` for the
    duration of the run and restores it afterwards.

    Prompts and outputs are recorded in full: reading the traces is the point
    (field-guide rule 10), and the documents here are fictional.
    """
    from mlflow.entities import SpanType

    from order_workflow.llm import LLMClient

    original = LLMClient.structured

    def traced(self, system: str, user: str, output_model):
        span_name = f"llm.{output_model.__name__}"
        with mlflow.start_span(name=span_name, span_type=SpanType.LLM) as span:
            span.set_inputs({"model": self.model, "system": system, "user": user})
            span.set_attribute("output_schema", output_model.__name__)
            try:
                parsed, usage = original(self, system, user, output_model)
            except Exception as exc:
                # A failed call is the most interesting thing in a trace; make
                # sure it is in there rather than swallowed by the re-raise.
                span.set_attribute("error", f"{type(exc).__name__}: {exc}")
                raise
            span.set_outputs({"parsed": parsed.model_dump(mode="json"),
                              "usage": usage.model_dump(mode="json")})
            span.set_attribute("cost_usd", usage.cost_usd)
            span.set_attribute("input_tokens", usage.input_tokens)
            span.set_attribute("output_tokens", usage.output_tokens)
            return parsed, usage

    LLMClient.structured = traced
    try:
        yield
    finally:
        LLMClient.structured = original


# ------------------------------------------------------------------- run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.run_mlflow", description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_DIR)
    parser.add_argument(
        "--case", default=None,
        help="Run a single case by id substring. For smoke tests: the run is logged like "
             "any other, so do not gate on it.",
    )
    parser.add_argument("--judge", action="store_true", help="Also run the LLM judge.")
    parser.add_argument("--dry-run", action="store_true", help="Print the cost estimate and stop.")
    parser.add_argument("--budget-usd", type=float, default=DEFAULT_BUDGET_USD)
    parser.add_argument("--experiment", default=None)
    parser.add_argument("--run-name", default=None)
    parser.add_argument(
        "--allow-over-budget", action="store_true",
        help="Run even when the dry estimate exceeds the budget. Off by default: fail closed.",
    )
    args = parser.parse_args(argv)

    config = load_config()
    llm_on = config.llm_enabled()

    est = estimate_cost(config, Path(args.cases), args.judge and llm_on)
    print_estimate(est, args.budget_usd)
    if not llm_on:
        print("  (no credentials configured: the suite will run on deterministic "
              "heuristics and the real cost will be $0.0000)\n")
    if args.dry_run:
        return 0
    over_budget = llm_on and est["estimated_total_cost_usd"] > args.budget_usd
    if over_budget and not args.allow_over_budget:
        print(f"\nRefusing to start: estimate ${est['estimated_total_cost_usd']:.4f} exceeds the "
              f"${args.budget_usd:.2f} budget. Re-run with --allow-over-budget to override.")
        return 2
    print()

    tracking.configure(experiment=args.experiment)
    # Enabled as the brief asks, and it is the right switch to have on: if the
    # client ever moves to messages.create, its spans appear for free. Today it
    # contributes nothing, because MLflow patches Messages.create while
    # LLMClient.structured calls Messages.parse, which goes straight to _post.
    # instrument_llm_client() below is what actually puts the LLM calls in the
    # traces; the two are complementary, not alternatives.
    mlflow.anthropic.autolog()

    @contextmanager
    def case_span(case_id: str, spec: dict):
        with mlflow.start_span(name=case_id) as span:
            span.set_inputs({"case": case_id, "category": spec.get("category"),
                             "input": spec.get("input")})
            yield
            span.set_outputs({"logged": True})

    run_name = args.run_name or f"suite-v{tracking.suite_version()['suite_version']}-" + (
        "llm" if llm_on else "heuristic"
    )
    with mlflow.start_run(run_name=run_name) as active:
        params = {
            "model": config.model,
            "judge_model": config.judge_model if args.judge else "not-run",
            # Both LLM steps use messages.parse, which takes no temperature.
            # Recording the value the request actually carried, not a wish.
            "temperature": "api-default",
            "max_tokens": config.max_tokens,
            "use_server_fallbacks": config.use_server_fallbacks,
            "llm_mode": "llm" if llm_on else "heuristic",
            "judge_enabled": args.judge and llm_on,
            "budget_usd": args.budget_usd,
            "estimated_cost_usd": est["estimated_total_cost_usd"],
            **tracking.prompt_hashes(),
            **tracking.suite_version(),
            **tracking.git_info(),
        }
        mlflow.log_params(params)

        judge_llm = get_llm(config, model=config.judge_model) if args.judge else None
        with instrument_llm_client():
            report = run_evals(
                config,
                cases_dir=args.cases,
                judge=args.judge,
                judge_llm=judge_llm,
                only_case=args.case,
                case_context=case_span,
            )

        # Traces are exported on a background queue. A short run can finish and
        # exit before the queue drains, which silently loses every span - so
        # drain it here, inside the run, rather than trusting interpreter exit.
        mlflow.flush_trace_async_logging()

        metrics = build_metrics(report)
        mlflow.log_metrics(metrics)

        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        # Stamp the run id into the report itself, so anything reading the
        # report later (the error analysis) names the run the numbers came
        # from rather than whichever run happened to finish last.
        report["mlflow_run_id"] = active.info.run_id
        report["mlflow_run_name"] = run_name
        report["suite_version"] = params["suite_version"]
        report["model"] = config.model
        (ARTIFACT_DIR / "report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (ARTIFACT_DIR / "cost_estimate.json").write_text(
            json.dumps(est, indent=2), encoding="utf-8"
        )
        write_tables(report["results"], ARTIFACT_DIR)
        mlflow.log_artifacts(str(ARTIFACT_DIR))

        # The gate reads this file; it is the run the thresholds were checked
        # against, so the pointer has to be written by the run itself.
        (REPO_ROOT / "evals" / "last_run.json").write_text(
            json.dumps(
                {
                    "run_id": active.info.run_id,
                    "experiment_id": active.info.experiment_id,
                    "run_name": run_name,
                    "logged_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "metrics": metrics,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        print(f"\nMLflow run id: {active.info.run_id}")
        print(f"Tracking store: {mlflow.get_tracking_uri()}")
        print(f"Actual LLM cost this run: ${metrics['cost_total_usd']:.4f} "
              f"(estimate was ${est['estimated_total_cost_usd']:.4f}, "
              f"budget ${args.budget_usd:.2f})")
        if metrics["cost_total_usd"] > args.budget_usd:
            print("[!] The run exceeded the budget. The number above is logged as "
                  "cost_total_usd; treat it as a defect in the estimate, not a rounding.")
    return 0


def build_metrics(report: dict) -> dict[str, float]:
    """Every number the gate is allowed to read, computed in one place."""
    results = report["results"]
    graded = [r for r in results if r["status"] in ("pass", "fail")]
    passed = [r for r in graded if r["status"] == "pass"]
    durations = sorted(r["duration_ms"] for r in graded)

    metrics: dict[str, float] = {
        "pass_rate": len(passed) / len(graded) if graded else 0.0,
        "cases_total": float(len(results)),
        "cases_graded": float(len(graded)),
        "cases_passed": float(len(passed)),
        "cases_failed": float(len(graded) - len(passed)),
        "cases_skipped": float(len(results) - len(graded)),
        "latency_mean_ms": statistics.mean(durations) if durations else 0.0,
        "latency_p95_ms": (
            durations[min(len(durations) - 1, math.ceil(0.95 * len(durations)) - 1)]
            if durations else 0.0
        ),
        "cost_total_usd": round(sum(r.get("cost_usd", 0.0) for r in graded), 6),
    }
    metrics["cost_per_case_usd"] = round(
        metrics["cost_total_usd"] / len(graded), 6
    ) if graded else 0.0

    for name, stats in report["category_scores"].items():
        metrics[f"pass_rate_{name}"] = stats["pass_rate"]
        metrics[f"cases_graded_{name}"] = float(stats["graded"])
    for name, stats in report["component_scores"].items():
        metrics[f"component_{name}_pass_rate"] = (
            stats["ok"] / stats["total"] if stats["total"] else 0.0
        )
    if report.get("judge"):
        scores = [j["score"] for j in report["judge"]]
        metrics["judge_mean_score"] = statistics.mean(scores)
        metrics["judge_cases"] = float(len(scores))
    return metrics


if __name__ == "__main__":
    sys.exit(main())
