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
