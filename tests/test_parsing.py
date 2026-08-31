from datetime import date

from order_workflow.parsing import (
    normalize_unit,
    parse_currency,
    parse_date,
    parse_number,
    to_tonnes,
)


def test_parse_number_formats():
    assert parse_number("1.234,50") == 1234.5      # Italian
    assert parse_number("1,234.50") == 1234.5      # English
    assert parse_number("1'234.50") == 1234.5      # Swiss
    assert parse_number("614,40") == 614.4
    assert parse_number("620") == 620.0
    assert parse_number("1.250") == 1250.0         # three digits after dot = thousands
    assert parse_number("0.5") == 0.5
    assert parse_number("12.5") == 12.5
    assert parse_number("1.234.567") == 1234567.0
    assert parse_number("€ 640,20") == 640.2
    assert parse_number("640.20 EUR") == 640.2
    assert parse_number("-3,5") == -3.5
    assert parse_number("") is None
    assert parse_number(None) is None
    assert parse_number("n/a") is None
    assert parse_number(25) == 25.0


def test_units():
    assert normalize_unit("T") == "t"
    assert normalize_unit("tonnellate") == "t"
    assert normalize_unit("Ton.") == "t"
    assert normalize_unit("KG") == "kg"
    assert normalize_unit(None) is None
    assert to_tonnes(40000, "kg") == 40.0
    assert to_tonnes(25, "t") == 25.0
    assert to_tonnes(25, None) == 25.0
    assert to_tonnes(None, "t") is None
    assert to_tonnes(10, "pezzi") is None  # unknown unit: cannot convert


def test_parse_date_formats():
    assert parse_date("2026-03-15") == date(2026, 3, 15)
    assert parse_date("15/03/2026") == date(2026, 3, 15)
    assert parse_date("15.03.2026") == date(2026, 3, 15)
    assert parse_date("15-03-2026") == date(2026, 3, 15)
    assert parse_date("15 marzo 2026") == date(2026, 3, 15)
    assert parse_date("March 15, 2026") == date(2026, 3, 15)
    assert parse_date("entro fine settembre") is None
    assert parse_date("31/02/2026") is None  # invalid day
    assert parse_date(None) is None


def test_parse_currency():
    assert parse_currency("€/t") == "EUR"
    assert parse_currency("EUR") == "EUR"
    assert parse_currency("chf") == "CHF"
    assert parse_currency("") is None
