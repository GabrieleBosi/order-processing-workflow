from order_workflow.config import Config
from order_workflow.models import NormalizedDocument, SourceType, Table
from order_workflow.steps import extract


def _doc(text="", tables=None, metadata=None) -> NormalizedDocument:
    return NormalizedDocument(
        source_file="t.txt", source_type=SourceType.TEXT,
        text=text, tables=tables or [], metadata=metadata or {},
    )


def _run(doc):
    order, _ = extract.run(doc, Config(llm_mode="stub"), llm=None)
    return order


def test_table_code_path():
    doc = _doc(
        text="Cliente: Acme S.p.A.\nRif: AB-12",
        tables=[Table(headers=["Codice", "Descrizione", "Qta", "UM", "Prezzo"],
                      rows=[["TND-B450C-12", "Tondo", "25", "t", "614,40"],
                            ["", "", "", "", ""]])],
    )
    order = _run(doc)
    assert order.extraction_method == "code"
    assert order.customer_name == "Acme S.p.A."
    assert order.order_ref == "AB-12"
    assert len(order.lines) == 1
    line = order.lines[0]
    assert line.sku == "TND-B450C-12"
    assert line.quantity == 25.0
    assert line.unit == "t"
    assert line.unit_price == 614.4


def test_heuristic_text_lines():
    order = _run(_doc(
        "Acme Steel S.r.l.\nOrdine XY-77\n"
        "- 25 t tondo B450C 12mm a 614,40 €/t\n"
        "- 40.000 kg vergella 6.5mm a 585,00 €/t\n"
        "Consegna: 15/10/2026\nCordiali saluti\n"
    ))
    assert order.extraction_method == "heuristic"
    assert order.customer_name == "Acme Steel S.r.l."
    assert order.order_ref == "XY-77"
    assert str(order.delivery_date) == "2026-10-15"
    assert len(order.lines) == 2
    assert order.lines[0].quantity == 25.0 and order.lines[0].unit == "t"
    assert order.lines[0].unit_price == 614.4
    assert order.lines[1].quantity == 40000.0 and order.lines[1].unit == "kg"


def test_spettle_is_not_the_customer():
    # "Spett.le X" is the supplier being addressed - never the customer.
    order = _run(_doc(
        "Spett.le Duferco Commerciale S.p.A.\n"
        "Acme Steel S.r.l.\n"
        "- 25 t tondo 12mm a 620 €/t\n"
    ))
    assert order.customer_name == "Acme Steel S.r.l."


def test_order_ref_not_taken_from_order_date():
    order = _run(_doc(
        "Acme S.p.A.\nData ordine: 01/09/2026\nOrdine n. AB-99\n- 25 t tondo 12mm\n"
    ))
    assert order.order_ref == "AB-99"
    assert str(order.order_date) == "2026-09-01"


def test_ref_requires_a_digit():
    order = _run(_doc("Acme S.p.A.\nin riferimento alla vostra offerta\n- 25 t tondo 12mm\n"))
    assert order.order_ref is None


def test_note_terms_attached_to_line():
    order = _run(_doc("Acme S.p.A.\n- 25 t tondo 12mm a 620 €/t, consegna urgente se possibile\n"))
    assert order.lines[0].notes is not None


def test_email_sender_used_as_customer():
    doc = _doc("please book order PO-1122\n- 10 t rebar 12mm at 620 EUR/t\n",
               metadata={"from": '"SteelTrade GmbH" <po@st.ch>', "subject": "order"})
    order = _run(doc)
    assert order.customer_name == "SteelTrade GmbH"
    assert order.order_ref == "PO-1122"


def test_signature_and_quoted_reply_ignored():
    order = _run(_doc(
        "Acme Steel S.r.l.\n"
        "- 20 t tondo 12mm a 620 €/t\n"
        "Cordiali saluti\nMario Rossi\nwww.acme.it\n"
        "> vi confermiamo 99 t disponibili\n"
    ))
    # the quoted "99 t" line must not become an order line
    assert len(order.lines) == 1
    assert order.lines[0].quantity == 20.0


def test_machine_numbers_from_excel_not_locale_guessed():
    # A typed Excel cell 24.375 is 24.375 t, never twenty-four thousand.
    doc = _doc(
        text="Cliente: Acme S.p.A.",
        tables=[Table(headers=["Codice", "Qta", "Prezzo"],
                      rows=[["TND-B450C-12", "24.375", "614.4"]],
                      numeric_source="machine")],
    )
    order = _run(doc)
    assert order.lines[0].quantity == 24.375
    assert order.lines[0].unit_price == 614.4


def test_four_digit_price_not_truncated():
    order = _run(_doc("Acme S.p.A.\n- 20 t lamiera 8mm a 1745,00 EUR/t\n"))
    assert order.lines[0].unit_price == 1745.0


def test_data_ordine_uppercase_not_order_ref():
    order = _run(_doc("Acme S.p.A.\nDATA ORDINE: 12/05/2026\nORDINE AB-77\n- 25 t tondo 12mm\n"))
    assert order.order_ref == "AB-77"


def test_vat_does_not_cross_newlines():
    order = _run(_doc("Acme S.p.A.\nP.IVA\n01234567890 non e' una partita iva qui\n- 25 t tondo 12mm\n"))
    assert order.customer_vat is None


def test_spettle_two_line_salutation_skipped():
    order = _run(_doc(
        "Spett.le\nDuferco Commerciale S.p.A.\n\nAcme Steel S.r.l.\n- 25 t tondo 12mm a 620 \u20ac/t\n"
    ))
    assert order.customer_name == "Acme Steel S.r.l."


# --------------------------------------------------------------- LLM path


class _FakeLLM:
    """Records every structured() call and answers by requested schema.

    The extraction step asks for the header and the lines separately because
    the combined schema is rejected by the API as too complex; this fake is
    what pins that shape in a test that costs nothing.
    """

    def __init__(self):
        self.calls = []

    def structured(self, system, user, output_model):
        from order_workflow.models import LLMUsage

        self.calls.append({"system": system, "user": user, "schema": output_model.__name__})
        if output_model is extract.LLMOrderHeader:
            parsed = extract.LLMOrderHeader(
                customer_name="Acme Steel S.r.l.", customer_vat="IT 01234 567890",
                order_ref="PO-2026-4501", order_date="2026-09-01",
                delivery_date="2026-10-15", currency="EUR", language="it",
                notes="consegna unica",
            )
        else:
            parsed = extract.LLMOrderLines(lines=[
                extract.LLMLine(description="Tondo B450C 12mm", sku="TND-B450C-12",
                                quantity=25.0, unit="t", unit_price=614.4, currency="EUR",
                                notes="urgente"),
            ])
        return parsed, LLMUsage(model="test-model", calls=1, input_tokens=100,
                                output_tokens=50, cost_usd=0.001)


def test_llm_path_asks_for_header_and_lines_separately():
    llm = _FakeLLM()
    order, usage = extract.run(_doc("Acme Steel S.r.l.\nordine a voce\n"), Config(), llm=llm)

    assert [c["schema"] for c in llm.calls] == ["LLMOrderHeader", "LLMOrderLines"]
    # Same prompt both times: the split is in the schema, never in the wording.
    assert llm.calls[0]["system"] == llm.calls[1]["system"] == extract.EXTRACTION_SYSTEM
    assert llm.calls[0]["user"] == llm.calls[1]["user"]

    # Every header fact survives the split, and both calls are billed.
    assert order.extraction_method == "llm"
    assert order.customer_name == "Acme Steel S.r.l."
    assert order.customer_vat == "IT01234567890"
    assert order.order_ref == "PO-2026-4501"
    assert str(order.order_date) == "2026-09-01"
    assert str(order.delivery_date) == "2026-10-15"
    assert order.currency == "EUR"
    assert order.language == "it"
    assert order.notes == "consegna unica"
    assert len(order.lines) == 1 and order.lines[0].sku == "TND-B450C-12"
    assert usage.calls == 2 and usage.cost_usd == 0.002
