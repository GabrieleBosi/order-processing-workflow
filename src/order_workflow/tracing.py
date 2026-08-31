"""Per-run tracing: every step's input and output, readable by a human.

Each run writes runs/<run_id>/ with one JSON file per step plus trace.md,
so a failed case can be replayed and inspected step by step.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from .models import PipelineRun, StepTrace

STEP_FILES = {
    1: "01_normalize.json",
    2: "02_extract.json",
    3: "03_reconcile.json",
    4: "04_check.json",
    5: "05_erp_write.json",
}


class TraceRecorder:
    def __init__(self, runs_dir: Path, run_id: str, enabled: bool = True):
        self.enabled = enabled
        self.run_dir = Path(runs_dir) / run_id
        if enabled:
            self.run_dir.mkdir(parents=True, exist_ok=True)

    def record(self, trace: StepTrace, output: BaseModel | None) -> None:
        if not self.enabled:
            return
        payload = {
            "trace": trace.model_dump(mode="json"),
            "output": output.model_dump(mode="json") if output is not None else None,
        }
        path = self.run_dir / STEP_FILES.get(trace.step, f"{trace.step:02d}_{trace.name}.json")
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def finalize(self, run: PipelineRun) -> None:
        if not self.enabled:
            return
        (self.run_dir / "run.json").write_text(
            run.model_dump_json(indent=2), encoding="utf-8"
        )
        (self.run_dir / "trace.md").write_text(render_trace_md(run), encoding="utf-8")


def render_trace_md(run: PipelineRun) -> str:
    lines = [
        f"# Run {run.run_id}",
        "",
        f"- **File**: `{run.source_file}`",
        f"- **Status**: {run.status.value}",
        f"- **Created**: {run.created_at.isoformat()}",
        "",
        "| # | Step | Status | Duration | LLM | Summary |",
        "|---|------|--------|----------|-----|---------|",
    ]
    for t in run.traces:
        llm = (
            f"{t.llm_usage.calls} call(s), ${t.llm_usage.cost_usd:.4f}"
            if t.llm_usage and t.llm_usage.calls
            else "-"
        )
        summary = (t.summary or t.error or "").replace("|", "/").replace("\n", " ")
        lines.append(f"| {t.step} | {t.name} | {t.status} | {t.duration_ms:.0f} ms | {llm} | {summary} |")
    total = run.total_llm_usage()
    lines += [
        "",
        (f"**Total LLM usage**: {total.calls} call(s), {total.input_tokens} in / "
         f"{total.output_tokens} out tokens, ${total.cost_usd:.4f}"),
        "",
    ]
    if run.checked:
        lines.append(f"**Check result**: {run.checked.summary}")
        for reason in run.checked.order_reasons:
            lines.append(f"- {reason}")
    if run.erp_result:
        lines.append(f"\n**ERP**: {run.erp_result.message}")
    return "\n".join(lines) + "\n"


def utcnow() -> datetime:
    return datetime.now(UTC)
