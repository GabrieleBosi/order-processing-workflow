import json
from datetime import date

import pytest

from order_workflow.config import REPO_ROOT
from order_workflow.erp import MockERP
from order_workflow.models import RunStatus
from order_workflow.pipeline import Pipeline

TODAY = date(2026, 9, 1)
CASES = REPO_ROOT / "evals" / "cases"


@pytest.fixture()
def pipeline(config, reference):
    return Pipeline(config, reference=reference, erp=MockERP(":memory:"), llm=None)


def test_e2e_email_to_erp(pipeline, config):
    run = pipeline.process(CASES / "case01_email_it_clean" / "input.eml", today=TODAY)
    assert run.status == RunStatus.AWAITING_CONFIRMATION
    assert run.checked.order_verdict.value == "auto_approve"
    assert run.erp_result is None  # guardrail: nothing written without confirmation

    run = pipeline.confirm(run)
    assert run.status == RunStatus.WRITTEN
    assert run.erp_result.erp_order_id.startswith("SO-")
    orders = pipeline.erp.list_orders()
    assert len(orders) == 1
    assert orders[0]["order_ref"] == "PO-2026-4501"
    assert len(orders[0]["lines"]) == 2

    # traces on disk, one file per step + human-readable trace
    run_dir = config.runs_dir / run.run_id
    names = {p.name for p in run_dir.iterdir()}
    assert {"01_normalize.json", "02_extract.json", "03_reconcile.json",
            "04_check.json", "05_erp_write.json", "trace.md", "run.json"} <= names
    step2 = json.loads((run_dir / "02_extract.json").read_text())
    assert step2["output"]["lines"], "step trace must carry readable output"


def test_rejected_order_cannot_be_confirmed(pipeline):
    run = pipeline.process(CASES / "case14_blocked_customer_txt" / "input.txt", today=TODAY)
    assert run.status == RunStatus.REJECTED
    with pytest.raises(ValueError):
        pipeline.confirm(run)
    assert pipeline.erp.list_orders() == []


def test_rejected_lines_are_skipped_on_write(pipeline):
    run = pipeline.process(CASES / "case10_price_blocking_csv" / "input.csv", today=TODAY)
    assert run.status == RunStatus.AWAITING_CONFIRMATION
    run = pipeline.confirm(run)
    orders = pipeline.erp.list_orders()
    assert len(orders[0]["lines"]) == 1  # the +10% price line was not written
    assert run.erp_result.skipped_line_nos == [1]


def test_double_confirm_is_refused(pipeline):
    run = pipeline.process(CASES / "case01_email_it_clean" / "input.eml", today=TODAY)
    pipeline.confirm(run)
    with pytest.raises(ValueError):
        pipeline.confirm(run)


def test_failed_run_reports_error(pipeline, tmp_path):
    bad = tmp_path / "order.docx"
    bad.write_text("x")
    run = pipeline.process(bad)
    assert run.status == RunStatus.FAILED
    assert run.error


def test_erp_ids_increment(pipeline):
    r1 = pipeline.confirm(pipeline.process(CASES / "case01_email_it_clean" / "input.eml", today=TODAY))
    r2 = pipeline.confirm(pipeline.process(CASES / "case03_csv_clean" / "input.csv", today=TODAY))
    assert r1.erp_result.erp_order_id != r2.erp_result.erp_order_id


def test_duplicate_ref_rechecked_at_write_time(pipeline):
    # The same PO processed twice before either confirm: the second confirm
    # must be refused, not double-booked.
    src = CASES / "case01_email_it_clean" / "input.eml"
    run1 = pipeline.process(src, today=TODAY)
    run2 = pipeline.process(src, today=TODAY)
    assert run2.checked.order_verdict.value == "auto_approve"  # nothing flagged yet
    pipeline.confirm(run1)
    run2 = pipeline.confirm(run2)
    assert run2.erp_result.erp_order_id is None
    assert "refused" in run2.erp_result.message
    assert len(pipeline.erp.list_orders()) == 1


def test_web_upload_trace_uses_display_name(pipeline, config):
    run = pipeline.process(
        CASES / "case01_email_it_clean" / "input.eml", today=TODAY,
        display_name="ordine_cliente.eml",
    )
    assert run.source_file == "ordine_cliente.eml"
    trace = (config.runs_dir / run.run_id / "trace.md").read_text()
    assert "ordine_cliente.eml" in trace
