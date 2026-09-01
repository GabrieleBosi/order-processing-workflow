"""Deterministic parsing helpers: numbers, units, dates.

Order documents mix Italian and English conventions ("1.234,50" vs
"1,234.50", "t" vs "kg", "15/03/2026" vs "March 15, 2026"). Everything
here is plain code: exact, free and testable.
"""

from __future__ import annotations

import re
from datetime import date

MONTHS = {
    # Italian
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5, "giugno": 6,
    "luglio": 7, "agosto": 8, "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
    # English
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}

TON_UNITS = {"t", "to", "ton", "tons", "tonne", "tonnes", "tonn", "tonnellate", "tonnellata", "mt", "ton."}
KG_UNITS = {"kg", "kgs", "chilogrammi", "kilograms"}


def parse_number(raw: str | float | None) -> float | None:
    """Parse a number in Italian or English notation.

    "1.234,50" -> 1234.5   "1,234.50" -> 1234.5   "1234.5" -> 1234.5
    A single separator followed by exactly three digits is grouping in both
    conventions ("1.250" -> 1250, "1,250" -> 1250) unless the integer part
    is 0. Three-decimal tonnages must arrive typed (Excel) or with an
    explicit decimal locale to be read as decimals.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip()
    if not s:
        return None
    s = re.sub(r"[€$\s]|EUR|USD|CHF", "", s, flags=re.IGNORECASE)
    s = s.replace("'", "")  # Swiss thousands separator 1'234.50
    if not re.search(r"\d", s):
        return None
    negative = s.startswith("-")
    s = s.lstrip("+-")

    has_comma = "," in s
    has_dot = "." in s
    try:
        if has_comma and has_dot:
            # Whichever separator comes last is the decimal mark.
            s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
        elif has_comma:
            # Same rule as the dot branch, mirrored: a single comma followed by
            # exactly three digits is grouping ("1,250 tons" = 1250), not an
            # Italian decimal, unless the integer part is 0.
            parts = s.split(",")
            is_thousands = len(parts) == 2 and len(parts[1]) == 3 and parts[0] != "0"
            s = s.replace(",", "") if (len(parts) > 2 or is_thousands) else s.replace(",", ".")
        elif has_dot:
            parts = s.split(".")
            # "1.234" or "12.345.678" -> thousands separators
            is_thousands = len(parts) == 2 and len(parts[1]) == 3 and parts[0] != "0" and len(parts[0]) <= 3
            if len(parts) > 2 or is_thousands:
                s = s.replace(".", "")
        value = float(s)
    except ValueError:
        return None
    return -value if negative else value


def normalize_unit(raw: str | None) -> str | None:
    if not raw:
        return None
    u = raw.strip().lower().rstrip(".")
    if u in TON_UNITS:
        return "t"
    if u in KG_UNITS:
        return "kg"
    return u or None


def to_tonnes(quantity: float | None, unit: str | None) -> float | None:
    """Convert a quantity to tonnes when the unit is known; None otherwise."""
    if quantity is None:
        return None
    u = normalize_unit(unit) or "t"
    if u == "t":
        return round(quantity, 3)
    if u == "kg":
        return round(quantity / 1000.0, 3)
    return None


DATE_PATTERNS = [
    re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"),                      # 2026-03-15
    re.compile(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b"),                # 15/03/2026, 15.03.2026
    re.compile(r"\b(\d{1,2})-(\d{1,2})-(\d{4})\b"),                      # 15-03-2026
]
TEXTUAL_DATE = re.compile(r"\b(\d{1,2})\s+([a-zA-Z]+)\s+(\d{4})\b")      # 15 marzo 2026
TEXTUAL_DATE_EN = re.compile(r"\b([a-zA-Z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b")  # March 15, 2026


def parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    s = str(raw).strip()
    m = DATE_PATTERNS[0].search(s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return _safe_date(y, mo, d)
    for pattern in DATE_PATTERNS[1:]:
        m = pattern.search(s)
        if m:
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if mo > 12 and d <= 12:  # tolerate US order
                d, mo = mo, d
            return _safe_date(y, mo, d)
    m = TEXTUAL_DATE.search(s)
    if m and m.group(2).lower() in MONTHS:
        return _safe_date(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)))
    m = TEXTUAL_DATE_EN.search(s)
    if m and m.group(1).lower() in MONTHS:
        return _safe_date(int(m.group(3)), MONTHS[m.group(1).lower()], int(m.group(2)))
    return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def find_date_in_text(text: str) -> date | None:
    return parse_date(text)


CURRENCY_MAP = {"€": "EUR", "eur": "EUR", "euro": "EUR", "$": "USD", "usd": "USD", "chf": "CHF"}


def parse_currency(raw: str | None) -> str | None:
    if not raw:
        return None
    s = raw.strip().lower()
    for key, val in CURRENCY_MAP.items():
        if key in s:
            return val
    return None
