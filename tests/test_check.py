from datetime import date

from order_workflow.config import Config
from order_workflow.models import (
    ExtractedLine,
    ExtractedOrder,
    LineVerdict,
    OrderVerdict,
)
from order_workflow.steps import check, reconcile

TODAY = date(2026, 9, 1)


def _checked(reference, erp, lines, customer="Acciaierie Rossi S.p.A.", **order_kw):
    order = ExtractedOrder(customer_name=customer, order_ref="PO-77",
                           delivery_date=order_kw.pop("delivery_date", date(2026, 10, 15)),
                           lines=lines, **order_kw)
    rec = reconcile.run(order, reference, erp, Config())
    result, _ = check.run(rec, Config(), llm=None, today=TODAY)
    return result


def _line(no=1, **kw):
    defaults = {"description": "tondo B450C 12mm", "quantity": 25.0, "unit": "t", "unit_price": 614.4}
    defaults.update(kw)
    return ExtractedLine(line_no=no, **defaults)


def test_clean_order_auto_approves(reference, erp):
    result = _checked(reference, erp, [_line()])
    assert result.lines[0].verdict == LineVerdict.APPROVE
    assert result.order_verdict == OrderVerdict.AUTO_APPROVE


def test_price_deviation_review_and_reject(reference, erp):
    result = _checked(reference, erp, [_line(unit_price=634.0), _line(no=2, unit_price=676.0)])
    assert result.lines[0].verdict == LineVerdict.REVIEW
    assert result.lines[1].verdict == LineVerdict.REJECT
    assert result.order_verdict == OrderVerdict.NEEDS_REVIEW


def test_note_escalates_to_review(reference, erp):
    result = _checked(reference, erp, [_line(notes="consegna urgente se possibile")])
    line = result.lines[0]
    assert line.verdict == LineVerdict.REVIEW
    assert line.llm_opinion is not None
    assert line.llm_opinion.source == "heuristic"  # no LLM configured


def test_opinion_cannot_downgrade(reference, erp):
    # A failed blocking rule stays a reject even if the model would approve.
    result = _checked(reference, erp, [_line(description="materiale ignoto xyz", sku=None)])
    assert result.lines[0].verdict == LineVerdict.REJECT


def test_past_delivery_date_review(reference, erp):
    result = _checked(reference, erp, [_line(delivery_date=date(2026, 8, 20))])
    assert result.lines[0].verdict == LineVerdict.REVIEW
    assert any("R5" in r for r in result.lines[0].reasons)


def test_order_level_date_applies_to_lines(reference, erp):
    result = _checked(reference, erp, [_line()], delivery_date=date(2026, 8, 20))
    assert result.lines[0].verdict == LineVerdict.REVIEW


def test_lead_time_too_short(reference, erp):
    result = _checked(reference, erp, [_line(delivery_date=date(2026, 9, 2))])
    assert result.lines[0].verdict == LineVerdict.REVIEW


def test_blocked_customer_rejects_order(reference, erp):
    result = _checked(reference, erp, [_line(unit_price=627.2)],
                      customer="Edilizia Colombo S.r.l.")
    assert result.order_verdict == OrderVerdict.REJECTED
    # line itself can still be fine - the block is order-level
    assert result.lines[0].verdict == LineVerdict.APPROVE


def test_all_lines_rejected_rejects_order(reference, erp):
    result = _checked(reference, erp, [_line(description="roba inesistente qwe", sku=None)])
    assert result.order_verdict == OrderVerdict.REJECTED
