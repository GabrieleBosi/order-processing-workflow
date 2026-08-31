"""Step 3 - reconcile against master data and flag exceptions (executor: queries).

Everything here is deterministic: SKU/alias/fuzzy lookups, price-list joins,
unit conversion, duplicate detection against the ERP. Exceptions are
*signalled*, not judged - deciding what they mean is step 4's job.
"""

from __future__ import annotations

from ..config import Config
from ..erp import MockERP
from ..models import (
    ExceptionCode,
    ExtractedOrder,
    OrderException,
    ReconciledLine,
    ReconciledOrder,
    Severity,
)
from ..parsing import to_tonnes
from ..reference import ReferenceData


def run(
    extracted: ExtractedOrder, reference: ReferenceData, erp: MockERP, config: Config
) -> ReconciledOrder:
    order = ReconciledOrder(extracted=extracted)

    # --- customer -------------------------------------------------------
    customer = reference.find_customer(extracted.customer_name, extracted.customer_vat)
    order.customer = customer
    if customer is None:
        order.exceptions.append(
            OrderException(
                code=ExceptionCode.UNKNOWN_CUSTOMER,
                severity=Severity.BLOCKING,
                message=f"Customer not found in master data: {extracted.customer_name!r} "
                        f"(VAT {extracted.customer_vat or 'n/a'})",
            )
        )
    elif customer.status.lower() != "active":
        order.exceptions.append(
            OrderException(
                code=ExceptionCode.CUSTOMER_BLOCKED,
                severity=Severity.BLOCKING,
                message=f"Customer {customer.name} ({customer.customer_id}) is {customer.status}.",
            )
        )

    # --- duplicate order reference (query against the ERP) --------------
    if (
        customer is not None
        and extracted.order_ref
        and erp.order_ref_exists(customer.customer_id, extracted.order_ref)
    ):
        order.exceptions.append(
            OrderException(
                code=ExceptionCode.DUPLICATE_ORDER_REF,
                severity=Severity.WARNING,
                message=f"Order reference {extracted.order_ref!r} already exists in the ERP "
                        f"for {customer.customer_id}.",
            )
        )

    # --- lines ----------------------------------------------------------
    total_value = 0.0
    any_value = False
    for ext in extracted.lines:
        line = ReconciledLine(extracted=ext)

        # Product: exact SKU -> alias -> fuzzy on description. Queries only.
        if ext.sku:
            hit = reference.find_product_by_sku(ext.sku)
            if hit:
                line.product, line.match_method = hit
                line.match_confidence = 1.0
        if line.product is None and ext.description:
            fuzzy = reference.search_product_by_description(ext.description)
            if fuzzy:
                line.product, line.match_confidence = fuzzy
                line.match_method = "fuzzy_name"
        if line.product is None and ext.sku and ext.description is not None:
            # Last resort: the SKU may be mistyped; try fuzzy on the SKU too.
            fuzzy = reference.search_product_by_description(ext.sku)
            if fuzzy and fuzzy[1] >= 0.75:
                line.product, line.match_confidence = fuzzy
                line.match_method = "fuzzy_sku"
        if line.product is None:
            line.exceptions.append(
                OrderException(
                    code=ExceptionCode.UNKNOWN_PRODUCT,
                    severity=Severity.BLOCKING,
                    message=f"No product match for line {ext.line_no}: "
                            f"sku={ext.sku!r} description={ext.description!r}",
                    line_no=ext.line_no,
                )
            )

        # Quantity: normalize to tonnes; kg->t conversions are flagged as info.
        if ext.quantity is None or ext.quantity <= 0:
            line.exceptions.append(
                OrderException(
                    code=ExceptionCode.MISSING_QUANTITY,
                    severity=Severity.BLOCKING,
                    message=f"Line {ext.line_no} has no usable quantity.",
                    line_no=ext.line_no,
                )
            )
        else:
            line.quantity_t = to_tonnes(ext.quantity, ext.unit)
            if ext.unit == "kg" and line.quantity_t is not None:
                line.exceptions.append(
                    OrderException(
                        code=ExceptionCode.UNIT_CONVERTED,
                        severity=Severity.INFO,
                        message=f"Line {ext.line_no}: {ext.quantity:g} kg converted to "
                                f"{line.quantity_t:g} t.",
                        line_no=ext.line_no,
                    )
                )
            elif line.quantity_t is None:
                line.quantity_t = round(ext.quantity, 3)  # assume tonnes, unit unknown

        # Price: join with the customer's agreed price list.
        if line.product is not None and customer is not None:
            line.expected_price = reference.expected_price(customer, line.product)
            if ext.unit_price is None:
                line.exceptions.append(
                    OrderException(
                        code=ExceptionCode.MISSING_PRICE,
                        severity=Severity.INFO,
                        message=f"Line {ext.line_no} has no price; agreed price "
                                f"{line.expected_price:.2f} EUR/t applies.",
                        line_no=ext.line_no,
                    )
                )
            elif line.expected_price > 0:
                delta = (ext.unit_price - line.expected_price) / line.expected_price * 100.0
                line.price_delta_pct = round(delta, 2)
                if abs(delta) > config.price_review_tolerance_pct:
                    severity = (
                        Severity.BLOCKING
                        if abs(delta) > config.price_block_tolerance_pct
                        else Severity.WARNING
                    )
                    line.exceptions.append(
                        OrderException(
                            code=ExceptionCode.PRICE_MISMATCH,
                            severity=severity,
                            message=f"Line {ext.line_no}: price {ext.unit_price:.2f} vs agreed "
                                    f"{line.expected_price:.2f} EUR/t ({delta:+.1f}%).",
                            line_no=ext.line_no,
                        )
                    )

        # MOQ (flagged here, judged in step 4).
        if (
            line.product is not None
            and line.quantity_t is not None
            and line.quantity_t < line.product.min_order_qty
        ):
            line.exceptions.append(
                OrderException(
                    code=ExceptionCode.BELOW_MOQ,
                    severity=Severity.WARNING,
                    message=f"Line {ext.line_no}: {line.quantity_t:g} t below minimum order "
                            f"quantity {line.product.min_order_qty:g} t for {line.product.sku}.",
                    line_no=ext.line_no,
                )
            )

        # Line value for the credit check.
        price = ext.unit_price if ext.unit_price is not None else line.expected_price
        if price is not None and line.quantity_t is not None:
            line.line_value = round(price * line.quantity_t, 2)
            total_value += line.line_value
            any_value = True

        order.lines.append(line)

    if any_value:
        order.order_value = round(total_value, 2)

    # --- credit limit ---------------------------------------------------
    if (
        customer is not None
        and order.order_value is not None
        and customer.credit_limit_eur > 0
        and order.order_value > customer.credit_limit_eur
    ):
        order.exceptions.append(
            OrderException(
                code=ExceptionCode.OVER_CREDIT_LIMIT,
                severity=Severity.WARNING,
                message=f"Order value {order.order_value:,.0f} EUR exceeds credit limit "
                        f"{customer.credit_limit_eur:,.0f} EUR for {customer.customer_id}.",
            )
        )

    if not extracted.lines:
        order.exceptions.append(
            OrderException(
                code=ExceptionCode.NO_VALID_LINES,
                severity=Severity.BLOCKING,
                message="No order lines were extracted from the document.",
            )
        )
    return order
