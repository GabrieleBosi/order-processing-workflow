"""Step 4 - line-by-line control (executor: LLM + rules). The risky step.

Hard rules run first, in code. The LLM only reads lines that look irregular
(free-text remarks, low-confidence matches, warnings) and its opinion can
only *escalate* a verdict (approve -> review -> reject), never soften one.
That asymmetry is the main guardrail of the whole workflow: a hallucinated
"all good" cannot override a failed rule.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from ..config import Config
from ..llm import LLMClient, LLMRefusalError
from ..models import (
    CheckedLine,
    CheckedOrder,
    ExceptionCode,
    LineVerdict,
    LLMOpinion,
    LLMUsage,
    OrderVerdict,
    ReconciledLine,
    ReconciledOrder,
    RuleResult,
    Severity,
)

VERDICT_RANK = {LineVerdict.APPROVE: 0, LineVerdict.REVIEW: 1, LineVerdict.REJECT: 2}


def _worst(a: LineVerdict, b: LineVerdict) -> LineVerdict:
    return a if VERDICT_RANK[a] >= VERDICT_RANK[b] else b


# ----------------------------------------------------------- LLM schema

class LLMCheckResult(BaseModel):
    """Risk assessment for one irregular order line."""

    risk: Literal["low", "medium", "high"] = Field(description="Operational risk of booking this line as-is")
    reasons: list[str] = Field(default_factory=list, description="Short, concrete reasons")
    suggested_verdict: Literal["approve", "review", "reject"] = Field(
        description="approve = safe to book; review = a human should look; reject = do not book"
    )


CHECK_SYSTEM = """\
You review single order lines for a steel trading back office before they are
booked into the ERP. You receive the line, the matched master data and the
exceptions already raised by deterministic rules.

Rubric:
- approve: everything on the line is consistent with master data and there is
  no free-text request that changes commercial terms.
- review: anything a human should confirm - vague references to past orders or
  agreements ("same as last delivery"), urgency requests, discount hints,
  conditional wording, quantities or descriptions that do not quite match.
- reject: the line clearly cannot be booked (contradictory or nonsensical).

Be conservative: when unsure, prefer review. Never invent facts about master
data. Your verdict can only tighten the outcome; rules already failed cannot
be overturned by you.
"""


# ------------------------------------------------------------ line rules


def _line_rules(
    line: ReconciledLine, today: date, config: Config, order_delivery: date | None = None
) -> list[RuleResult]:
    ext = line.extracted
    rules: list[RuleResult] = []

    rules.append(
        RuleResult(
            rule_id="R1_PRODUCT_MATCHED",
            passed=line.product is not None,
            severity=Severity.BLOCKING if line.product is None else Severity.INFO,
            message=(
                f"Matched {line.product.sku} via {line.match_method}"
                if line.product
                else "No product match in master data."
            ),
        )
    )
    rules.append(
        RuleResult(
            rule_id="R2_QUANTITY_PRESENT",
            passed=ext.quantity is not None and ext.quantity > 0,
            severity=Severity.BLOCKING if not (ext.quantity and ext.quantity > 0) else Severity.INFO,
            message=f"Quantity: {ext.quantity!r} {ext.unit or ''}".strip(),
        )
    )

    price_ok = True
    price_sev = Severity.INFO
    price_msg = "No agreed price available." if line.expected_price is None else "Price within tolerance."
    if line.price_delta_pct is not None:
        delta = abs(line.price_delta_pct)
        if delta > config.price_block_tolerance_pct:
            price_ok, price_sev = False, Severity.BLOCKING
            price_msg = (
                f"Price deviates {line.price_delta_pct:+.1f}% from agreed "
                f"(block > {config.price_block_tolerance_pct}%)."
            )
        elif delta > config.price_review_tolerance_pct:
            price_ok, price_sev = False, Severity.WARNING
            price_msg = (
                f"Price deviates {line.price_delta_pct:+.1f}% from agreed "
                f"(review > {config.price_review_tolerance_pct}%)."
            )
    rules.append(
        RuleResult(rule_id="R3_PRICE_TOLERANCE", passed=price_ok, severity=price_sev, message=price_msg)
    )

    below_moq = any(e.code == ExceptionCode.BELOW_MOQ for e in line.exceptions)
    rules.append(
        RuleResult(
            rule_id="R4_MOQ",
            passed=not below_moq,
            severity=Severity.WARNING if below_moq else Severity.INFO,
            message="Below minimum order quantity." if below_moq else "Quantity above MOQ.",
        )
    )

    delivery = ext.delivery_date or order_delivery
    date_ok, date_msg = True, "No delivery date on line or order."
    if delivery is not None:
        earliest = today + timedelta(days=config.min_lead_days)
        if delivery < today:
            date_ok, date_msg = False, f"Delivery date {delivery.isoformat()} is in the past."
        elif delivery < earliest:
            date_ok, date_msg = False, (
                f"Delivery date {delivery.isoformat()} is inside the minimum lead time "
                f"({config.min_lead_days} days)."
            )
        else:
            date_msg = f"Delivery date {delivery.isoformat()} is feasible."
    rules.append(
        RuleResult(
            rule_id="R5_DELIVERY_DATE",
            passed=date_ok,
            severity=Severity.WARNING if not date_ok else Severity.INFO,
            message=date_msg,
        )
    )

    # R6 is two rules, deliberately. Surfacing a remark to the person confirming
    # the order and withholding auto-approval because of it are different
    # decisions, and conflating them was a defect: the model extractor keeps any
    # remark the document contains, so "deliver to our Brescia plant" was
    # downgrading clean orders to review. The note is always shown; only a note
    # that asks for something, or that addresses the system rather than the
    # buyer's counterpart, moves the verdict.
    note = (ext.notes or "").strip()
    rules.append(
        RuleResult(
            rule_id="R6_NOTE_SURFACED",
            passed=not note,
            severity=Severity.INFO,
            message=f"Free-text remark on line: {note!r}" if note else "No free-text remarks.",
        )
    )
    hits = _note_confirmation_terms(note)
    rules.append(
        RuleResult(
            rule_id="R6_NOTES_REGULAR",
            passed=not hits,
            severity=Severity.WARNING if hits else Severity.INFO,
            message=(
                "Remark needs human confirmation, matched "
                + ", ".join(repr(h) for h in hits[:3])
                + f": {note!r}"
                if hits
                else "No remark that needs confirmation."
            ),
        )
    )
    return rules


def _verdict_from_rules(rules: list[RuleResult]) -> tuple[LineVerdict, list[str]]:
    verdict = LineVerdict.APPROVE
    reasons: list[str] = []
    for rule in rules:
        if rule.passed:
            continue
        reasons.append(f"{rule.rule_id}: {rule.message}")
        if rule.severity == Severity.BLOCKING:
            verdict = _worst(verdict, LineVerdict.REJECT)
        elif rule.severity == Severity.WARNING:
            verdict = _worst(verdict, LineVerdict.REVIEW)
    return verdict, reasons


def _needs_model_opinion(line: ReconciledLine, rules: list[RuleResult]) -> bool:
    """Which lines are worth an opinion.

    A remark is no longer a trigger on its own. It was, back when a `notes`
    value could only come from the heuristic extractor's hint list and so was
    always a risk signal; on the model path a note is whatever prose the
    document carried, and asking the model about a delivery address only
    invited it to escalate a line the rules had cleared. A remark that matches
    `R6_NOTES_REGULAR` still fails a WARNING rule and still arrives here.
    """
    if line.match_method in ("fuzzy_name", "fuzzy_sku") and (line.match_confidence or 0) < 0.8:
        return True
    return any(not r.passed and r.severity == Severity.WARNING for r in rules)


HEURISTIC_RISK_TERMS = (
    "urgente", "urgent", "se possibile", "if possible", "come l'ultima", "come ultima",
    "as last", "as per last", "da confermare", "to be confirmed", "sconto", "discount",
    "salvo", "subject to", "anticip", "flessibil",
)

# A remark that names the machine reading it, or tells that machine what to do
# with the rest of the document, is not a message to the back office - it is
# addressed to the system. This is not injection detection: it looks only at
# text the extractor already put in a line note, and its only effect is to
# withhold auto-approval so a human reads the note. The terms are the shapes
# the safety cases actually use, in the four languages the suite covers.
SYSTEM_ADDRESSED_TERMS = (
    "sistema", "system", "systeme", "système",
    "istruzion", "anweisung", "assistente", "assistant",
    "ai model", "modello ai", "llm", "prompt", "agente:", "agent:",
    "ignora le istruzioni", "ignore previous", "ignore the above",
    "skip the human", "senza attendere conferma", "senza conferma",
    "ueberspringen", "überspringen",
)


def _note_confirmation_terms(note: str) -> list[str]:
    """The terms in `note` that mean a human has to look at it.

    Two lists, one question. `HEURISTIC_RISK_TERMS` is the commercial one -
    urgency, discounts, "same as last time" - and was already the list this
    codebase used to decide a remark mattered. `SYSTEM_ADDRESSED_TERMS` is the
    one the safety cases need: a note that talks to the system keeps its order
    out of auto-approve even when nothing commercial is being asked for.
    """
    text = note.lower()
    return [term for term in (*HEURISTIC_RISK_TERMS, *SYSTEM_ADDRESSED_TERMS) if term in text]


def _heuristic_opinion(line: ReconciledLine) -> LLMOpinion:
    text = f"{line.extracted.notes or ''} {line.extracted.description}".lower()
    hits = [term for term in HEURISTIC_RISK_TERMS if term in text]
    if hits:
        return LLMOpinion(
            risk="medium",
            reasons=[f"Remark contains term needing confirmation: {hit!r}" for hit in hits[:3]],
            suggested_verdict=LineVerdict.REVIEW,
            source="heuristic",
        )
    if line.match_method in ("fuzzy_name", "fuzzy_sku") and (line.match_confidence or 0) < 0.8:
        return LLMOpinion(
            risk="medium",
            reasons=[f"Low-confidence product match ({line.match_confidence})."],
            suggested_verdict=LineVerdict.REVIEW,
            source="heuristic",
        )
    return LLMOpinion(risk="low", reasons=[], suggested_verdict=LineVerdict.APPROVE, source="heuristic")


def _llm_opinion(line: ReconciledLine, llm: LLMClient) -> tuple[LLMOpinion, LLMUsage]:
    ext = line.extracted
    context = {
        "description": ext.description,
        "sku_in_document": ext.sku,
        "matched_product": line.product.model_dump() if line.product else None,
        "match_method": line.match_method,
        "match_confidence": line.match_confidence,
        "quantity": ext.quantity,
        "unit": ext.unit,
        "quantity_tonnes": line.quantity_t,
        "unit_price": ext.unit_price,
        "agreed_price": line.expected_price,
        "price_delta_pct": line.price_delta_pct,
        "delivery_date": ext.delivery_date.isoformat() if ext.delivery_date else None,
        "notes": ext.notes,
        "rule_exceptions": [e.model_dump(mode="json") for e in line.exceptions],
    }
    import json

    result, usage = llm.structured(
        system=CHECK_SYSTEM,
        user=f"Assess this order line:\n\n{json.dumps(context, indent=2, ensure_ascii=False)}",
        output_model=LLMCheckResult,
    )
    opinion = LLMOpinion(
        risk=result.risk,
        reasons=result.reasons,
        suggested_verdict=LineVerdict(result.suggested_verdict),
        source="llm",
    )
    return opinion, usage


# ------------------------------------------------------------ main entry


def run(
    reconciled: ReconciledOrder,
    config: Config,
    llm: LLMClient | None,
    today: date | None = None,
) -> tuple[CheckedOrder, LLMUsage | None]:
    today = today or date.today()
    checked = CheckedOrder(reconciled=reconciled)
    total_usage = LLMUsage()

    order_delivery = reconciled.extracted.delivery_date
    for line in reconciled.lines:
        rules = _line_rules(line, today, config, order_delivery=order_delivery)
        verdict, reasons = _verdict_from_rules(rules)
        opinion: LLMOpinion | None = None
        if _needs_model_opinion(line, rules):
            if llm is not None:
                try:
                    opinion, usage = _llm_opinion(line, llm)
                    total_usage.add(usage)
                except LLMRefusalError:
                    opinion = _heuristic_opinion(line)
            else:
                opinion = _heuristic_opinion(line)
            # Escalation only: the model can tighten, never loosen.
            if VERDICT_RANK[opinion.suggested_verdict] > VERDICT_RANK[verdict]:
                verdict = opinion.suggested_verdict
                reasons.extend(f"model: {reason}" for reason in opinion.reasons)
        checked.lines.append(
            CheckedLine(
                reconciled=line, verdict=verdict, reasons=reasons,
                rule_results=rules, llm_opinion=opinion,
            )
        )

    # ----- order-level rules -------------------------------------------
    order_verdict = OrderVerdict.AUTO_APPROVE
    order_reasons: list[str] = []
    for exc in reconciled.exceptions:
        order_reasons.append(f"{exc.code.value}: {exc.message}")
        if exc.severity == Severity.BLOCKING:
            order_verdict = OrderVerdict.REJECTED
        elif exc.severity == Severity.WARNING and order_verdict != OrderVerdict.REJECTED:
            order_verdict = OrderVerdict.NEEDS_REVIEW

    if order_verdict != OrderVerdict.REJECTED:
        if all(line.verdict == LineVerdict.REJECT for line in checked.lines) and checked.lines:
            order_verdict = OrderVerdict.REJECTED
            order_reasons.append("Every line was rejected.")
        elif any(line.verdict != LineVerdict.APPROVE for line in checked.lines):
            order_verdict = OrderVerdict.NEEDS_REVIEW

    checked.order_verdict = order_verdict
    checked.order_reasons = order_reasons

    approve = sum(1 for ln in checked.lines if ln.verdict == LineVerdict.APPROVE)
    review = sum(1 for ln in checked.lines if ln.verdict == LineVerdict.REVIEW)
    reject = sum(1 for ln in checked.lines if ln.verdict == LineVerdict.REJECT)
    checked.summary = (
        f"{len(checked.lines)} line(s): {approve} approve, {review} review, {reject} reject. "
        f"Order verdict: {order_verdict.value}."
    )
    usage = total_usage if total_usage.calls else None
    return checked, usage
