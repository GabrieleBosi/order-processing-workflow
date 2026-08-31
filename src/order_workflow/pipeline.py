"""The Level-2 workflow: five fixed steps, no agentic branching.

The sequence is stable, so an orchestrated workflow beats an agent here:
every run takes the same path, which makes runs comparable, traceable and
evaluable. `process` runs steps 1-4 and always stops before the ERP;
`confirm` is the human gate that runs step 5.
"""

from __future__ import annotations

import time
import uuid
from datetime import date
from pathlib import Path

from .config import Config
from .erp import MockERP
from .llm import LLMClient, get_llm
from .models import LLMUsage, OrderVerdict, PipelineRun, RunStatus, StepTrace
from .reference import ReferenceData
from .steps import check, erp_write, extract, normalize, reconcile
from .tracing import TraceRecorder, utcnow


class Pipeline:
    def __init__(
        self,
        config: Config,
        reference: ReferenceData | None = None,
        erp: MockERP | None = None,
        llm: LLMClient | None = None,
        trace: bool = True,
    ):
        self.config = config
        self.reference = reference or ReferenceData(config.reference_dir)
        self.erp = erp or MockERP(config.erp_db_path)
        self.llm = llm if llm is not None else get_llm(config)
        self.trace_enabled = trace

    # ------------------------------------------------------------- steps

    def process(self, path: Path | str, today: date | None = None) -> PipelineRun:
        """Run steps 1-4. Never writes to the ERP."""
        run = PipelineRun(
            run_id=uuid.uuid4().hex[:12],
            source_file=str(path),
            created_at=utcnow(),
        )
        recorder = TraceRecorder(self.config.runs_dir, run.run_id, enabled=self.trace_enabled)
        try:
            run.normalized = self._step(
                run, recorder, 1, "normalize",
                lambda: (normalize.run(path), None),
                summarize=lambda d: (
                    f"{d.source_type.value}: {len(d.text)} chars, {len(d.tables)} table(s)"
                    + (", OCR used" if d.ocr_used else "")
                    + (f", {len(d.warnings)} warning(s)" if d.warnings else "")
                ),
            )
            run.extracted = self._step(
                run, recorder, 2, "extract",
                lambda: extract.run(run.normalized, self.config, self.llm),
                summarize=lambda o: (
                    f"{len(o.lines)} line(s) via {o.extraction_method}; customer="
                    f"{o.customer_name!r}, ref={o.order_ref!r}"
                ),
            )
            run.reconciled = self._step(
                run, recorder, 3, "reconcile",
                lambda: (reconcile.run(run.extracted, self.reference, self.erp, self.config), None),
                summarize=lambda r: (
                    f"customer={'OK:' + r.customer.customer_id if r.customer else 'NOT FOUND'}, "
                    f"{sum(1 for line in r.lines if line.product)} of {len(r.lines)} line(s) matched, "
                    f"{len(r.all_exceptions())} exception(s), value={r.order_value}"
                ),
            )
            run.checked = self._step(
                run, recorder, 4, "check",
                lambda: check.run(run.reconciled, self.config, self.llm, today=today),
                summarize=lambda c: c.summary,
            )
            run.status = (
                RunStatus.REJECTED
                if run.checked.order_verdict == OrderVerdict.REJECTED
                else RunStatus.AWAITING_CONFIRMATION
            )
        except Exception as exc:  # noqa: BLE001 - a run must always be reportable
            run.status = RunStatus.FAILED
            run.error = f"{type(exc).__name__}: {exc}"
        recorder.finalize(run)
        return run

    def confirm(self, run: PipelineRun) -> PipelineRun:
        """Step 5, only after a human said yes. Rejected orders never reach the ERP."""
        if run.checked is None or run.status not in (RunStatus.AWAITING_CONFIRMATION,):
            raise ValueError(f"Run {run.run_id} is not awaiting confirmation (status={run.status.value}).")
        recorder = TraceRecorder(self.config.runs_dir, run.run_id, enabled=self.trace_enabled)
        run.erp_result = self._step(
            run, recorder, 5, "erp_write",
            lambda: (erp_write.run(run.checked, self.erp), None),
            summarize=lambda r: r.message,
        )
        run.status = RunStatus.WRITTEN if run.erp_result.erp_order_id else RunStatus.REJECTED
        recorder.finalize(run)
        return run

    # ---------------------------------------------------------- plumbing

    def _step(self, run: PipelineRun, recorder: TraceRecorder, number: int, name: str, fn, summarize):
        started = utcnow()
        t0 = time.monotonic()
        try:
            result = fn()
            output, usage = result if isinstance(result, tuple) else (result, None)
            if usage is not None and not isinstance(usage, LLMUsage):
                usage = None
            trace = StepTrace(
                step=number, name=name, status="ok", started_at=started,
                duration_ms=(time.monotonic() - t0) * 1000,
                summary=summarize(output), llm_usage=usage,
            )
            run.traces.append(trace)
            recorder.record(trace, output)
            return output
        except Exception as exc:
            trace = StepTrace(
                step=number, name=name, status="error", started_at=started,
                duration_ms=(time.monotonic() - t0) * 1000,
                error=f"{type(exc).__name__}: {exc}",
            )
            run.traces.append(trace)
            recorder.record(trace, None)
            raise
