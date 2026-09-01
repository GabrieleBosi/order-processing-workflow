"""Step 5 - write into the ERP (executor: code), behind human confirmation.

The pipeline never calls this on its own: `Pipeline.process` stops at
`awaiting_confirmation` and only `Pipeline.confirm` - triggered by a person
(CLI flag typed by a human, confirm button in the app) - reaches this step.
Rejected orders cannot be written at all.
"""

from __future__ import annotations

from ..erp import MockERP
from ..models import CheckedOrder, ERPWriteResult, ExceptionCode, OrderVerdict


def run(checked: CheckedOrder, erp: MockERP) -> ERPWriteResult:
    all_line_nos = [line.reconciled.extracted.line_no for line in checked.lines]
    if checked.order_verdict == OrderVerdict.REJECTED:
        return ERPWriteResult(
            erp_order_id=None,
            written_lines=0,
            skipped_line_nos=all_line_nos,
            message="Order is rejected; nothing was written to the ERP.",
        )
    # Re-check the duplicate guard at write time: the same PO processed twice
    # in parallel would pass reconcile cleanly in both runs, and the second
    # confirm must not book it again. A duplicate the reviewer already saw
    # (flagged at reconcile time) and confirmed anyway is allowed through.
    customer = checked.reconciled.customer
    order_ref = checked.reconciled.extracted.order_ref
    if customer and order_ref and erp.order_ref_exists(customer.customer_id, order_ref):
        seen_at_check = any(
            e.code == ExceptionCode.DUPLICATE_ORDER_REF for e in checked.reconciled.exceptions
        )
        if not seen_at_check:
            return ERPWriteResult(
                erp_order_id=None,
                written_lines=0,
                skipped_line_nos=all_line_nos,
                message=f"Write refused: order reference {order_ref!r} was booked for "
                        f"{customer.customer_id} after this run was checked. Reprocess the document.",
            )
    return erp.write_order(checked)
