"""Shared data contracts for the order-processing workflow.

Every step consumes and produces one of these models, so a run can be
traced, replayed and evaluated step by step.
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class SourceType(str, enum.Enum):
    EMAIL = "email"
    PDF = "pdf"
    EXCEL = "excel"
    CSV = "csv"
    TEXT = "text"


class Severity(str, enum.Enum):
    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"


class ExceptionCode(str, enum.Enum):
    UNKNOWN_CUSTOMER = "UNKNOWN_CUSTOMER"
    CUSTOMER_BLOCKED = "CUSTOMER_BLOCKED"
    UNKNOWN_PRODUCT = "UNKNOWN_PRODUCT"
    AMBIGUOUS_PRODUCT = "AMBIGUOUS_PRODUCT"
    PRICE_MISMATCH = "PRICE_MISMATCH"
    BELOW_MOQ = "BELOW_MOQ"
    OVER_CREDIT_LIMIT = "OVER_CREDIT_LIMIT"
    DUPLICATE_ORDER_REF = "DUPLICATE_ORDER_REF"
    INVALID_DELIVERY_DATE = "INVALID_DELIVERY_DATE"
    UNIT_CONVERTED = "UNIT_CONVERTED"
    MISSING_QUANTITY = "MISSING_QUANTITY"
    MISSING_PRICE = "MISSING_PRICE"
    IRREGULAR_NOTE = "IRREGULAR_NOTE"
    NO_VALID_LINES = "NO_VALID_LINES"
    OCR_UNAVAILABLE = "OCR_UNAVAILABLE"


class LineVerdict(str, enum.Enum):
    APPROVE = "approve"
    REVIEW = "review"
    REJECT = "reject"


class OrderVerdict(str, enum.Enum):
    AUTO_APPROVE = "auto_approve"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"


class RunStatus(str, enum.Enum):
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    WRITTEN = "written"
    REJECTED = "rejected"
    FAILED = "failed"


# --------------------------------------------------------------------------
# Step 1 - normalize
# --------------------------------------------------------------------------


class Table(BaseModel):
    name: str = ""
    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)


class NormalizedDocument(BaseModel):
    source_file: str
    source_type: SourceType
    text: str = ""
    tables: list[Table] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    ocr_used: bool = False


# --------------------------------------------------------------------------
# Step 2 - extract
# --------------------------------------------------------------------------


class ExtractedLine(BaseModel):
    line_no: int
    description: str = ""
    sku: str | None = None
    quantity: float | None = None
    unit: str | None = None
    unit_price: float | None = None
    currency: str | None = None
    delivery_date: date | None = None
    notes: str | None = None


class ExtractedOrder(BaseModel):
    customer_name: str | None = None
    customer_vat: str | None = None
    order_ref: str | None = None
    order_date: date | None = None
    delivery_date: date | None = None
    currency: str = "EUR"
    lines: list[ExtractedLine] = Field(default_factory=list)
    notes: str | None = None
    language: str | None = None
    extraction_method: Literal["code", "llm", "heuristic"] = "code"


# --------------------------------------------------------------------------
# Step 3 - reconcile
# --------------------------------------------------------------------------


class OrderException(BaseModel):
    code: ExceptionCode
    severity: Severity
    message: str
    line_no: int | None = None


class MatchedProduct(BaseModel):
    sku: str
    name: str
    unit: str = "t"
    min_order_qty: float = 0.0
    list_price: float = 0.0


class MatchedCustomer(BaseModel):
    customer_id: str
    name: str
    status: str = "active"
    credit_limit_eur: float = 0.0
    discount_pct: float = 0.0
    payment_terms: str = ""


class ReconciledLine(BaseModel):
    extracted: ExtractedLine
    product: MatchedProduct | None = None
    match_method: str | None = None  # exact_sku | alias | fuzzy_name
    match_confidence: float | None = None
    quantity_t: float | None = None  # quantity normalized to tonnes
    expected_price: float | None = None
    price_delta_pct: float | None = None
    line_value: float | None = None
    exceptions: list[OrderException] = Field(default_factory=list)


class ReconciledOrder(BaseModel):
    extracted: ExtractedOrder
    customer: MatchedCustomer | None = None
    lines: list[ReconciledLine] = Field(default_factory=list)
    exceptions: list[OrderException] = Field(default_factory=list)  # order-level
    order_value: float | None = None

    def all_exceptions(self) -> list[OrderException]:
        out = list(self.exceptions)
        for line in self.lines:
            out.extend(line.exceptions)
        return out


# --------------------------------------------------------------------------
# Step 4 - check
# --------------------------------------------------------------------------


class RuleResult(BaseModel):
    rule_id: str
    passed: bool
    severity: Severity = Severity.INFO
    message: str = ""


class LLMOpinion(BaseModel):
    risk: Literal["low", "medium", "high"]
    reasons: list[str] = Field(default_factory=list)
    suggested_verdict: LineVerdict
    source: Literal["llm", "heuristic"] = "llm"


class CheckedLine(BaseModel):
    reconciled: ReconciledLine
    verdict: LineVerdict
    reasons: list[str] = Field(default_factory=list)
    rule_results: list[RuleResult] = Field(default_factory=list)
    llm_opinion: LLMOpinion | None = None


class CheckedOrder(BaseModel):
    reconciled: ReconciledOrder
    lines: list[CheckedLine] = Field(default_factory=list)
    order_verdict: OrderVerdict = OrderVerdict.NEEDS_REVIEW
    order_reasons: list[str] = Field(default_factory=list)
    summary: str = ""


# --------------------------------------------------------------------------
# Step 5 - ERP write
# --------------------------------------------------------------------------


class ERPWriteResult(BaseModel):
    erp_order_id: str | None = None
    written_lines: int = 0
    skipped_line_nos: list[int] = Field(default_factory=list)
    message: str = ""


# --------------------------------------------------------------------------
# Tracing / run container
# --------------------------------------------------------------------------


class LLMUsage(BaseModel):
    model: str = ""
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, other: LLMUsage) -> None:
        self.model = self.model or other.model
        self.calls += other.calls
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cost_usd = round(self.cost_usd + other.cost_usd, 6)


class StepTrace(BaseModel):
    step: int
    name: str
    status: Literal["ok", "error", "skipped"] = "ok"
    started_at: datetime
    duration_ms: float = 0.0
    summary: str = ""
    llm_usage: LLMUsage | None = None
    error: str | None = None


class PipelineRun(BaseModel):
    run_id: str
    source_file: str
    created_at: datetime
    status: RunStatus = RunStatus.AWAITING_CONFIRMATION
    normalized: NormalizedDocument | None = None
    extracted: ExtractedOrder | None = None
    reconciled: ReconciledOrder | None = None
    checked: CheckedOrder | None = None
    erp_result: ERPWriteResult | None = None
    traces: list[StepTrace] = Field(default_factory=list)
    error: str | None = None

    def total_llm_usage(self) -> LLMUsage:
        total = LLMUsage()
        for trace in self.traces:
            if trace.llm_usage:
                total.add(trace.llm_usage)
        return total
