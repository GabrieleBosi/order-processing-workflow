"""Build the human labelling sheet for the judge cases of a logged run.

    python -m evals.label_sheet                       # the run in evals/last_run.json
    python -m evals.label_sheet --run-id <id>         # a specific MLflow run

Field-guide rule 11: an LLM judge may only score subjective quality, and only
after it has been calibrated against human labels. Calibration needs a human to
score the same cases the judge scored, and to do that a human needs, in one
place and per case: the document that arrived, what the pipeline extracted from
it, and what the judge said about that extraction.

That is exactly what this writes to reports/label_sheet.md. Nothing here is a
judgement or a summary: every section is copied out of the run's own MLflow
trace - the judge call's prompt holds the source document and the extraction it
was shown, and the judge span holds the score and the rationale it returned.

The scores you fill in go in evals/labels.jsonl (`human_score`, 1-5, against
evals/rubrics/extraction_fidelity.md), and `python -m evals.calibrate_judge`
then computes Cohen's kappa. Read the document and score it BEFORE reading the
judge's rationale: a judge you have already agreed with is not a measurement.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import tracking

DEFAULT_REPORT = tracking.REPO_ROOT / "evals" / "_artifacts" / "report.json"
DEFAULT_OUT = tracking.REPO_ROOT / "reports" / "label_sheet.md"
LAST_RUN_PATH = tracking.REPO_ROOT / "evals" / "last_run.json"
LABELS_PATH = tracking.REPO_ROOT / "evals" / "labels.jsonl"
RUBRIC_PATH = tracking.REPO_ROOT / "evals" / "rubrics" / "extraction_fidelity.md"

SOURCE_MARKER = "--- SOURCE DOCUMENT ---"
EXTRACTION_MARKER = "--- EXTRACTION ---"


def _read_last_run() -> dict:
    if not LAST_RUN_PATH.is_file():
        raise SystemExit(f"no {LAST_RUN_PATH}: run `python -m evals.run_mlflow --judge` first")
    return json.loads(LAST_RUN_PATH.read_text(encoding="utf-8"))


def judge_prompts(run_id: str, experiment_id: str) -> dict[str, dict[str, str]]:
    """Per case: the source document and the extraction the judge was shown.

    Both come out of the judge call's own prompt, which the eval runner records
    in full on the span - so the sheet shows what was actually judged rather
    than something reconstructed afterwards and hoped to be the same.
    """
    import mlflow

    tracking.configure()
    traces = mlflow.search_traces(
        run_id=run_id, locations=[str(experiment_id)], return_type="list"
    )
    out: dict[str, dict[str, str]] = {}
    for trace in traces:
        for span in trace.data.spans:
            if span.name != "llm.JudgeResult":
                continue
            user = (span.inputs or {}).get("user", "")
            if SOURCE_MARKER not in user or EXTRACTION_MARKER not in user:
                continue
            head, _, rest = user.partition(SOURCE_MARKER)
            source, _, extraction = rest.partition(EXTRACTION_MARKER)
            # The root span of a judge trace is named "<case_id>::judge".
            case_id = next(
                (s.name.split("::")[0] for s in trace.data.spans if s.name.endswith("::judge")),
                None,
            )
            if case_id is None:
                continue
            out[case_id] = {
                "focus": head.replace("Focus:", "").strip(),
                "source": source.strip(),
                "extraction": extraction.strip(),
                "model": (span.inputs or {}).get("model", ""),
            }
    return out


def _existing_labels() -> dict[str, dict]:
    if not LABELS_PATH.is_file():
        return {}
    rows = {}
    for line in LABELS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("//"):
            row = json.loads(line)
            rows[row["case"]] = row
    return rows


def render(report: dict, prompts: dict[str, dict[str, str]], run_info: dict) -> str:
    judged = report.get("judge", [])
    labels = _existing_labels()
    by_case = {r["case"]: r for r in report["results"]}
    params = run_info.get("params", {})

    lines: list[str] = ["# Labelling sheet", ""]
    lines.append(
        f"Judge cases of MLflow run `{run_info['run_id']}` "
        f"(`{run_info.get('run_name', '')}`), model "
        f"`{params.get('model') or report.get('model', '?')}`, "
        f"judge `{params.get('judge_model', '?')}`, "
        f"suite version {params.get('suite_version') or report.get('suite_version', '?')}."
    )
    lines += [
        "",
        f"{len(judged)} case(s) carry a `judge` block and were scored in this run. Every section "
        "below is copied out of that run's trace: the source document and the extraction are the "
        "ones the judge was actually shown, and the score and rationale are the ones it returned.",
        "",
        "## How to use this",
        "",
        f"1. Read the source document and the extraction, and score fidelity 1-5 against "
        f"`{RUBRIC_PATH.relative_to(tracking.REPO_ROOT).as_posix()}` - **before** reading the "
        "judge's score. A human who has already seen the judge's answer is not an independent "
        "label, and kappa against it means nothing.",
        f"2. Write your score into `{LABELS_PATH.relative_to(tracking.REPO_ROOT).as_posix()}` as "
        "`human_score` for that case, with a short `note` when you disagree with the judge.",
        "3. Run `python -m evals.calibrate_judge` to log Cohen's kappa onto the run. Until that "
        "metric exists, `evals/gate.py` enforces no judge threshold at all.",
        "",
        "The rubric's 1-5 scale is the same for both scorers. Judge only fidelity to the "
        "document - whether the order is bookable is what the objective checks are for.",
        "",
        "---",
        "",
    ]

    for i, entry in enumerate(judged, start=1):
        case_id = entry["case"]
        prompt = prompts.get(case_id, {})
        result = by_case.get(case_id, {})
        prior = labels.get(case_id, {})
        human = prior.get("human_score")
        lines.append(f"## {i}. `{case_id}`")
        lines.append("")
        lines.append(
            f"- category: `{entry.get('category', '')}`  |  objective checks: "
            f"**{result.get('status', '?')}**  |  extraction method: "
            f"`{result.get('extraction_method', '?')}`"
        )
        lines.append(f"- judge focus: {prompt.get('focus') or '(not recorded)'}")
        if result.get("failed"):
            lines.append(f"- failing objective checks: {'; '.join(result['failed'])}")
        lines.append("")
        lines.append("### Source document (as shown to the judge)")
        lines.append("")
        lines.append("```text")
        lines.append(prompt.get("source", "(no trace found for this case)"))
        lines.append("```")
        lines.append("")
        lines.append("### ExtractedOrder from this run")
        lines.append("")
        lines.append("```json")
        lines.append(prompt.get("extraction", "(no trace found for this case)"))
        lines.append("```")
        lines.append("")
        lines.append(f"### Judge: **{entry['score']}/5**")
        lines.append("")
        lines.append(f"> {entry['rationale']}")
        lines.append("")
        lines.append("### Human label")
        lines.append("")
        lines.append(
            f"- `human_score`: **{human if human is not None else '____'}**  "
            f"(1-5, fill in `evals/labels.jsonl`)"
        )
        lines.append(f"- note: {prior.get('note') or '____'}")
        lines.append("")
        lines.append("---")
        lines.append("")

    filled = sum(1 for e in judged if labels.get(e["case"], {}).get("human_score") is not None)
    lines.append(f"**{filled} of {len(judged)} labels filled in.** "
                 "`judge_kappa` cannot be computed until all of them are.")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.label_sheet", description=__doc__)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    report = json.loads(args.report.read_text(encoding="utf-8"))
    last = _read_last_run()
    run_id = args.run_id or report.get("mlflow_run_id") or last["run_id"]
    run_info = {"run_id": run_id, "run_name": report.get("mlflow_run_name", last.get("run_name"))}

    if not report.get("judge"):
        print(f"{args.report} holds no judge results: re-run with --judge.")
        return 1

    prompts = judge_prompts(run_id, last["experiment_id"])
    import mlflow

    run_info["params"] = dict(mlflow.get_run(run_id).data.params)
    missing = [e["case"] for e in report["judge"] if e["case"] not in prompts]
    if missing:
        print(f"[!] no judge trace found for {len(missing)} case(s): {', '.join(missing)}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(report, prompts, run_info), encoding="utf-8")
    print(f"Wrote {len(report['judge'])} case section(s) to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
