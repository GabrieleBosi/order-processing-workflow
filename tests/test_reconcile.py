from datetime import date

from order_workflow.config import Config
from order_workflow.models import ExceptionCode, ExtractedLine, ExtractedOrder
from order_workflow.steps import reconcile


def _order(customer="Acciaierie Rossi S.p.A.", ref="PO-1", lines=None, **kw) -> ExtractedOrder:
    return ExtractedOrder(
        customer_name=customer, order_ref=ref,
        delivery_date=date(2026, 10, 15),
        lines=lines or [], **kw,
    )


def _line(no=1, **kw) -> ExtractedLine:
    defaults = {"description": "tondo B450C 12mm", "quantity": 25.0, "unit": "t", "unit_price": 614.4}
    defaults.update(kw)
    return ExtractedLine(line_no=no, **defaults)


def codes(rec):
    return {e.code for e in rec.all_exceptions()}


def test_exact_sku_and_agreed_price(reference, erp):
    rec = reconcile.run(_order(lines=[_line(sku="TND-B450C-12", description="")]),
                        reference, erp, Config())
    line = rec.lines[0]
    assert rec.customer.customer_id == "C001"
    assert line.product.sku == "TND-B450C-12"
    assert line.match_method == "exact_sku"
    assert line.expected_price == 614.4  # 640 - 4%
    assert line.price_delta_pct == 0.0
    assert line.line_value == 15360.0


def test_fuzzy_description_match(reference, erp):
    rec = reconcile.run(_order(lines=[_line()]), reference, erp, Config())
    assert rec.lines[0].product.sku == "TND-B450C-12"
    assert rec.lines[0].match_method == "fuzzy_name"


def test_unknown_product_blocks(reference, erp):
    rec = reconcile.run(_order(lines=[_line(description="pannelli fotovoltaici", sku=None)]),
                        reference, erp, Config())
    assert rec.lines[0].product is None
    assert ExceptionCode.UNKNOWN_PRODUCT in codes(rec)


def test_kg_conversion_flagged(reference, erp):
    rec = reconcile.run(_order(lines=[_line(quantity=40000, unit="kg")]),
                        reference, erp, Config())
    assert rec.lines[0].quantity_t == 40.0
    assert ExceptionCode.UNIT_CONVERTED in codes(rec)


def test_price_mismatch_severities(reference, erp):
    cfg = Config()
    # +3.2% -> warning
    rec = reconcile.run(_order(lines=[_line(unit_price=634.0)]), reference, erp, cfg)
    exc = [e for e in rec.lines[0].exceptions if e.code == ExceptionCode.PRICE_MISMATCH]
    assert exc and exc[0].severity.value == "warning"
    # +10% -> blocking
    rec = reconcile.run(_order(lines=[_line(unit_price=676.0)]), reference, erp, cfg)
    exc = [e for e in rec.lines[0].exceptions if e.code == ExceptionCode.PRICE_MISMATCH]
    assert exc and exc[0].severity.value == "blocking"


def test_below_moq(reference, erp):
    rec = reconcile.run(_order(lines=[_line(quantity=5)]), reference, erp, Config())
    assert ExceptionCode.BELOW_MOQ in codes(rec)


def test_over_credit_limit(reference, erp):
    # C006 Officine Marchetti: limit 80k, no discount
    rec = reconcile.run(
        _order(customer="Officine Marchetti S.n.c.",
               lines=[_line(description="travi HEB 200", unit_price=750.0, quantity=200)]),
        reference, erp, Config(),
    )
    assert rec.customer.customer_id == "C006"
    assert ExceptionCode.OVER_CREDIT_LIMIT in codes(rec)


def test_duplicate_ref_query(reference, erp):
    erp.db.execute(
        "INSERT INTO sales_orders VALUES ('SO-X','PO-1','C001','','EUR',0,'open','2026-01-01')"
    )
    rec = reconcile.run(_order(lines=[_line(sku="TND-B450C-12")]), reference, erp, Config())
    assert ExceptionCode.DUPLICATE_ORDER_REF in codes(rec)


def test_unknown_customer_and_vat_priority(reference, erp):
    rec = reconcile.run(_order(customer="Sconosciuti S.r.l."), reference, erp, Config())
    assert rec.customer is None
    assert ExceptionCode.UNKNOWN_CUSTOMER in codes(rec)
    # VAT wins even with a garbled name
    rec = reconcile.run(
        _order(customer="???", customer_vat="IT09876543210", lines=[_line()]),
        reference, erp, Config(),
    )
    assert rec.customer.customer_id == "C002"


def test_blocked_customer(reference, erp):
    rec = reconcile.run(_order(customer="Edilizia Colombo S.r.l."), reference, erp, Config())
    assert ExceptionCode.CUSTOMER_BLOCKED in codes(rec)


def test_no_lines_flagged(reference, erp):
    rec = reconcile.run(_order(lines=[]), reference, erp, Config())
    assert ExceptionCode.NO_VALID_LINES in codes(rec)


def test_unknown_unit_flagged_not_assumed(reference, erp):
    rec = reconcile.run(_order(lines=[_line(quantity=50, unit="pz")]), reference, erp, Config())
    assert rec.lines[0].quantity_t is None
    assert ExceptionCode.UNIT_UNKNOWN in codes(rec)


def test_price_below_agreement_symmetric(reference, erp):
    # agreed 614.40 for C001; -3.2% -> warning, -10% -> blocking
    rec = reconcile.run(_order(lines=[_line(unit_price=594.6)]), reference, erp, Config())
    exc = [e for e in rec.lines[0].exceptions if e.code == ExceptionCode.PRICE_MISMATCH]
    assert exc and exc[0].severity.value == "warning"
    rec = reconcile.run(_order(lines=[_line(unit_price=552.9)]), reference, erp, Config())
    exc = [e for e in rec.lines[0].exceptions if e.code == ExceptionCode.PRICE_MISMATCH]
    assert exc and exc[0].severity.value == "blocking"


def test_wrong_size_never_matched(reference, erp):
    # 14mm rebar is not in the catalogue; it must NOT match the 12mm product.
    rec = reconcile.run(_order(lines=[_line(description="tondo B450C 14mm", sku=None)]),
                        reference, erp, Config())
    assert rec.lines[0].product is None
    assert ExceptionCode.UNKNOWN_PRODUCT in codes(rec)


def test_lookalike_product_capped_below_review_gate(reference, erp):
    # HEA 200 (not in catalogue) may fuzzy-hit HEB 200, but confidence must
    # stay under the 0.8 gate so step 4 forces a review instead of approving.
    hit = reference.search_product_by_description("Travi HEA 200 S275JR")
    if hit is not None:
        product, confidence, ambiguous = hit
        assert confidence < 0.8


def test_fuzzy_customer_match_flagged_uncertain(reference, erp):
    rec = reconcile.run(_order(customer="Acciaierie Rosi Srl", lines=[_line()]),
                        reference, erp, Config())
    if rec.customer is not None:
        assert ExceptionCode.CUSTOMER_MATCH_UNCERTAIN in codes(rec)
