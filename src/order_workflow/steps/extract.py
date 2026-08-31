"""Step 2 - extract order lines (executor: LLM, with a code path).

Design rule from the workflow: use code wherever the input is regular.
Structured tables (CSV/Excel) are extracted by column mapping - exact,
free and testable. The LLM is reserved for irregular free text (email
bodies, PDFs). Without credentials a deterministic heuristic extractor
stands in, so the pipeline always runs end to end.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from ..config import Config
from ..llm import LLMClient, LLMRefusalError
from ..models import ExtractedLine, ExtractedOrder, LLMUsage, NormalizedDocument, Table
from ..parsing import normalize_unit, parse_currency, parse_date, parse_number

# ------------------------------------------------------------ column maps

COLUMN_SYNONYMS: dict[str, set[str]] = {
    "sku": {"codice", "cod", "cod.", "codice articolo", "articolo", "art", "art.", "sku", "item",
            "item code", "code", "product code", "codice prodotto"},
    "description": {"descrizione", "desc", "desc.", "materiale", "prodotto", "description",
                    "material", "product", "designazione"},
    "quantity": {"qta", "qtà", "q.tà", "quantita", "quantità", "qty", "quantity", "tons",
                 "tonnellate", "peso", "weight"},
    "unit": {"um", "u.m", "u.m.", "unit", "uom", "unita", "unità"},
    "unit_price": {"prezzo", "prezzo unitario", "prezzo unit", "price", "unit price", "€/t",
                   "eur/t", "prezzo €/t", "price eur/t", "prezzo eur/t"},
    "delivery_date": {"consegna", "data consegna", "delivery", "delivery date", "resa"},
    "notes": {"note", "notes", "osservazioni", "remarks"},
}


def _map_headers(table: Table) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for idx, header in enumerate(table.headers):
        key = header.strip().lower().rstrip(".:")
        for field, synonyms in COLUMN_SYNONYMS.items():
            if field not in mapping and (key in synonyms or header.strip().lower() in synonyms):
                mapping[field] = idx
    return mapping


def table_is_order_table(table: Table) -> bool:
    mapping = _map_headers(table)
    return "quantity" in mapping and ("sku" in mapping or "description" in mapping)


# ---------------------------------------------------------- LLM schemas

class LLMLine(BaseModel):
    """One order line as read from the document (verbatim values, no guessing)."""

    description: str = Field(description="Product description as written in the document")
    sku: str | None = Field(default=None, description="Product/article code if present, verbatim")
    quantity: float | None = Field(default=None, description="Ordered quantity as a number")
    unit: str | None = Field(default=None, description="Unit as written, e.g. t, ton, kg")
    unit_price: float | None = Field(default=None, description="Unit price as a number, no separators")
    currency: str | None = Field(default=None, description="Currency code or symbol if present")
    delivery_date: str | None = Field(default=None, description="Line delivery date in ISO YYYY-MM-DD")
    notes: str | None = Field(default=None, description="Free-text remarks attached to this line")


class LLMOrder(BaseModel):
    customer_name: str | None = Field(default=None, description="Ordering company name")
    customer_vat: str | None = Field(default=None, description="VAT number if present")
    order_ref: str | None = Field(default=None, description="Customer order/PO reference")
    order_date: str | None = Field(default=None, description="Order date in ISO YYYY-MM-DD")
    delivery_date: str | None = Field(
        default=None, description="Requested delivery date for the whole order, ISO"
    )
    currency: str | None = Field(default=None, description="Order currency, e.g. EUR")
    language: str | None = Field(default=None, description="Document language code, e.g. it, en")
    notes: str | None = Field(default=None, description="Order-level free-text remarks")
    lines: list[LLMLine] = Field(default_factory=list)


EXTRACTION_SYSTEM = """\
You extract purchase-order data from steel-trading documents (Italian or English).
Rules:
- The customer is the party PLACING the order: the email sender or the letterhead
  company. "Spett.le X" / "To: X" name the supplier, never the customer.
- Report values exactly as written; do not invent or complete missing fields (use null).
- One output line per ordered item. Ignore signatures, disclaimers and quoted reply history.
- Quantities and prices must be plain numbers (1234.5, never "1.234,50").
- Dates must be ISO YYYY-MM-DD; if a date is ambiguous or partial, use null.
- Keep any unusual request or remark attached to a line in that line's notes field.
"""


# ------------------------------------------------------------- main entry


def run(
    doc: NormalizedDocument, config: Config, llm: LLMClient | None
) -> tuple[ExtractedOrder, LLMUsage | None]:
    order_tables = [t for t in doc.tables if table_is_order_table(t)]
    if order_tables:
        order = _extract_from_tables(doc, order_tables)
        order.extraction_method = "code"
        return order, None
    if llm is not None:
        try:
            return _extract_with_llm(doc, llm)
        except LLMRefusalError:
            pass  # degrade to the heuristic path below
    order = _extract_heuristic(doc)
    order.extraction_method = "heuristic"
    return order, None


# ------------------------------------------------------------- code path


def _extract_from_tables(doc: NormalizedDocument, tables: list[Table]) -> ExtractedOrder:
    order = ExtractedOrder()
    _fill_header_from_text(order, doc)
    line_no = 0
    for table in tables:
        mapping = _map_headers(table)
        for row in table.rows:
            def cell(field: str, row: list[str] = row, mapping: dict[str, int] = mapping) -> str | None:
                idx = mapping.get(field)
                if idx is None or idx >= len(row):
                    return None
                value = row[idx].strip()
                return value or None

            if not any((cell("sku"), cell("description"), cell("quantity"))):
                continue
            line_no += 1
            currency = parse_currency(cell("unit_price") or "") or order.currency
            order.lines.append(
                ExtractedLine(
                    line_no=line_no,
                    description=cell("description") or "",
                    sku=cell("sku"),
                    quantity=parse_number(cell("quantity")),
                    unit=normalize_unit(cell("unit")) or _unit_from_header(table, mapping),
                    unit_price=parse_number(cell("unit_price")),
                    currency=currency,
                    delivery_date=parse_date(cell("delivery_date")),
                    notes=cell("notes"),
                )
            )
    return order


def _unit_from_header(table: Table, mapping: dict[str, int]) -> str | None:
    idx = mapping.get("quantity")
    if idx is None:
        return None
    header = table.headers[idx].lower()
    if re.search(r"\b(t|ton|tons|tonnellate)\b", header):
        return "t"
    if "kg" in header:
        return "kg"
    return None


HEADER_PATTERNS = {
    # Keywords are case-insensitive; the captured reference is not, and must
    # contain a digit, so prose after "ordine"/"rif" is never mistaken for a
    # reference. "data ordine" (order date) is explicitly excluded.
    "order_ref": re.compile(
        r"(?<![Dd]ata )(?i:rif(?:erimento)?|ordine\s*(?:di acquisto)?\s*(?:n[.°ro]*)?"
        r"|order\s*(?:ref(?:erence)?|no|n[.°]|number)?|po)"
        r"\s*[:.]?\s*((?=[A-Z0-9/-]*\d)[A-Z][A-Z0-9/-]{2,}|\d[A-Z0-9/-]{4,})"
    ),
    "vat": re.compile(
        r"(?:p\.?\s*iva|vat(?:\s*number)?)\s*[:.]?\s*([A-Z]{0,3}\s?\d[\d\s]{6,})", re.IGNORECASE
    ),
    # NB: "Spett.le X" names the SUPPLIER (the addressee), never the customer.
    "customer": re.compile(
        r"(?:ragione sociale|cliente|customer|mittente|company)\s*[:.]\s*(.+)",
        re.IGNORECASE,
    ),
    "delivery": re.compile(r"(?:consegna|delivery|resa)[^:\n]*[:.]?\s*(.+)", re.IGNORECASE),
    "order_date": re.compile(r"(?:data ordine|order date|data|date)\s*[:.]?\s*(.+)", re.IGNORECASE),
}
SIGNATURE_PATTERN = re.compile(
    r"(?:cordiali saluti|distinti saluti|best regards|kind regards|saluti)[\s,]*\n+"
    r"(?:.*\n)?(.*(?:S\.?p\.?A|S\.?r\.?l|GmbH|AG\b|S\.?n\.?c|Ltd|SA\b).*)",
    re.IGNORECASE,
)
COMPANY_SUFFIX = re.compile(r"\b(S\.?p\.?A\.?|S\.?r\.?l\.?|S\.?n\.?c\.?|GmbH|AG|Ltd\.?|SA)\b")


def _clean_company(name: str) -> str:
    """Trim trailing punctuation but keep the dot of 'S.p.A.' / 'S.r.l.'."""
    name = name.strip().rstrip(",;").strip()
    if name.endswith(".") and not COMPANY_SUFFIX.search(name[-8:]):
        name = name.rstrip(".")
    return name


def _fill_header_from_text(order: ExtractedOrder, doc: NormalizedDocument) -> None:
    text = doc.text or ""
    subject = doc.metadata.get("subject", "")
    sender = doc.metadata.get("from", "")
    haystack = f"{subject}\n{text}"

    m = HEADER_PATTERNS["order_ref"].search(haystack)
    if m:
        order.order_ref = m.group(1).strip().rstrip(".,;")
    m = HEADER_PATTERNS["vat"].search(haystack)
    if m:
        order.customer_vat = re.sub(r"\s", "", m.group(1))
    m = HEADER_PATTERNS["customer"].search(haystack)
    if m:
        order.customer_name = _clean_company(m.group(1))
    elif sender:
        # Email: the customer is the sender.
        display = re.sub(r"<[^>]*>", "", sender).strip().strip('"')
        if display and "@" not in display:
            order.customer_name = display
    if order.customer_name is None:
        # Letterhead: the first line naming a company is the ordering party -
        # skip lines addressed to the supplier ("Spett.le ...").
        for candidate in text.splitlines()[:6]:
            stripped = candidate.strip()
            if not stripped or stripped.lower().startswith("spett"):
                continue
            if COMPANY_SUFFIX.search(stripped):
                order.customer_name = _clean_company(stripped)
                break
    if order.customer_name is None:
        m = SIGNATURE_PATTERN.search(text)
        if m:
            order.customer_name = _clean_company(m.group(1))
    m = HEADER_PATTERNS["delivery"].search(haystack)
    if m:
        order.delivery_date = parse_date(m.group(1))
    m = HEADER_PATTERNS["order_date"].search(haystack)
    if m:
        order.order_date = parse_date(m.group(1))


# -------------------------------------------------------------- LLM path


def _render_document(doc: NormalizedDocument) -> str:
    parts: list[str] = []
    if doc.metadata.get("subject"):
        parts.append(f"Subject: {doc.metadata['subject']}")
    if doc.metadata.get("from"):
        parts.append(f"From: {doc.metadata['from']}")
    if doc.text:
        parts.append(doc.text)
    for table in doc.tables:
        parts.append(f"[Table: {table.name}]")
        parts.append(" | ".join(table.headers))
        parts.extend(" | ".join(row) for row in table.rows)
    return "\n".join(parts)


def _extract_with_llm(doc: NormalizedDocument, llm: LLMClient) -> tuple[ExtractedOrder, LLMUsage]:
    raw, usage = llm.structured(
        system=EXTRACTION_SYSTEM,
        user=f"Extract the purchase order from this document:\n\n{_render_document(doc)}",
        output_model=LLMOrder,
    )
    order = ExtractedOrder(
        customer_name=raw.customer_name,
        customer_vat=re.sub(r"\s", "", raw.customer_vat) if raw.customer_vat else None,
        order_ref=raw.order_ref,
        order_date=parse_date(raw.order_date),
        delivery_date=parse_date(raw.delivery_date),
        currency=parse_currency(raw.currency) or "EUR",
        language=raw.language,
        notes=raw.notes,
        extraction_method="llm",
    )
    for i, line in enumerate(raw.lines, start=1):
        order.lines.append(
            ExtractedLine(
                line_no=i,
                description=line.description,
                sku=line.sku,
                quantity=line.quantity,
                unit=normalize_unit(line.unit),
                unit_price=line.unit_price,
                currency=parse_currency(line.currency),
                delivery_date=parse_date(line.delivery_date),
                notes=line.notes,
            )
        )
    return order, usage


# -------------------------------------------------------- heuristic path

SKU_TOKEN = re.compile(r"\b([A-Z]{2,4}-[A-Z0-9][A-Z0-9.\-]{1,15})\b")
QTY_UNIT = re.compile(
    r"(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?|\d+(?:[.,]\d+)?)\s*(t|ton[sn]?|tonnellate|kg)\b",
    re.IGNORECASE,
)
PRICE_TOKEN = re.compile(
    r"(?:@|a|al prezzo di|prezzo|price(?:\s+of)?|per)?\s*"
    r"(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?)\s*(?:€|eur|euro)\s*(?:/\s*(?:t|ton))?",
    re.IGNORECASE,
)
NOTE_HINT = re.compile(
    r"(urgente|urgent|se possibile|if possible|come (?:l')?ultima|as (?:per )?last|da confermare|"
    r"to be confirmed|sconto|discount|salvo|flessibil|anticip)",
    re.IGNORECASE,
)
STOP_LINE = re.compile(
    r"(cordiali|distinti|regards|saluti|firma|disclaimer|confidenzial|www\.|@.*\.(com|it|ch))",
    re.IGNORECASE,
)


def _extract_heuristic(doc: NormalizedDocument) -> ExtractedOrder:
    """Deterministic text extraction used when no LLM is configured.

    Looks for lines carrying a quantity+unit pair; picks up SKU, price and
    remarks from the same line. Good enough for regular text, and honest
    about it: irregular documents are exactly where the LLM earns its keep.
    """
    order = ExtractedOrder()
    _fill_header_from_text(order, doc)
    line_no = 0
    for raw_line in (doc.text or "").splitlines():
        if raw_line.lstrip().startswith(">"):  # quoted reply history
            continue
        line = raw_line.strip().lstrip("-*•").strip()
        if not line or STOP_LINE.search(line):
            continue
        qty_match = QTY_UNIT.search(line)
        if not qty_match:
            continue
        line_no += 1
        quantity = parse_number(qty_match.group(1))
        unit = normalize_unit(qty_match.group(2))
        sku_match = SKU_TOKEN.search(line)
        price = None
        for price_match in PRICE_TOKEN.finditer(line):
            candidate = parse_number(price_match.group(1))
            if candidate is not None and candidate != quantity:
                price = candidate
        description = line
        description = QTY_UNIT.sub(" ", description)
        description = PRICE_TOKEN.sub(" ", description)
        if sku_match:
            description = description.replace(sku_match.group(1), " ")
        description = re.sub(r"\s{2,}", " ", description).strip(" -–—:;,.@")
        note_match = NOTE_HINT.search(line)
        order.lines.append(
            ExtractedLine(
                line_no=line_no,
                description=description,
                sku=sku_match.group(1) if sku_match else None,
                quantity=quantity,
                unit=unit,
                unit_price=price,
                currency="EUR" if price is not None else None,
                delivery_date=(
                    parse_date(line)
                    if "consegna" in line.lower() or "delivery" in line.lower()
                    else None
                ),
                notes=line if note_match else None,
            )
        )
    return order
