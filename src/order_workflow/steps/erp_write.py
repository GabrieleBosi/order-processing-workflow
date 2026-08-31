"""Step 5 - write into the ERP (executor: code), behind human confirmation.

The pipeline never calls this on its own: `Pipeline.process` stops at
`awaiting_confirmation` and only `Pipeline.confirm` - triggered by a person
(CLI flag typed by a human, confirm button in the app) - reaches this step.
Rejected orders cannot be written at all.
"""

from __future__ import annotations

from ..erp import MockERP
from ..models import CheckedOrder, ERPWriteResult, OrderVerdict


def run(checked: CheckedOrder, erp: MockERP) -> ERPWriteResult:
    if checked.order_verdict == OrderVerdict.REJECTED:
        return ERPWriteResult(
            erp_order_id=None,
            written_lines=0,
            skipped_line_nos=[line.reconciled.extracted.line_no for line in checked.lines],
            message="Order is rejected; nothing was written to the ERP.",
        )
    return erp.write_order(checked)
