"""Generate the eval set (evals/cases/) and demo samples (data/samples/).

One source of truth for every eval case: input document + hand-authored
ground truth (case.json). Generated artifacts are committed, so users never
need to run this; re-run it only when changing the case set.

    python scripts/generate_data.py
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "evals" / "cases"
SAMPLES = ROOT / "data" / "samples"

TODAY = "2026-09-01"  # frozen clock for deterministic date rules


def eml(from_name: str, from_addr: str, subject: str, body: str) -> str:
    return (
        f'From: "{from_name}" <{from_addr}>\n'
        f"To: ordini@duferco-commerciale.example\n"
        f"Subject: {subject}\n"
        f"Date: Mon, 31 Aug 2026 10:00:00 +0200\n"
        f"MIME-Version: 1.0\n"
        f"Content-Type: text/plain; charset=utf-8\n\n{body}"
    )


def write_case(name: str, input_name: str, content: str | bytes, spec: dict) -> Path:
    case_dir = CASES / name
    case_dir.mkdir(parents=True, exist_ok=True)
    path = case_dir / input_name
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    spec.setdefault("today", TODAY)
    spec["input"] = input_name
    (case_dir / "case.json").write_text(
        json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return path


def make_xlsx(path_or_rows, rows: list[list] | None = None) -> bytes:
    from io import BytesIO

    from openpyxl import Workbook

    rows = rows if rows is not None else path_or_rows
    wb = Workbook()
    ws = wb.active
    ws.title = "Ordine"
    for row in rows:
        ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def make_pdf(lines: list[str]) -> bytes:
    from io import BytesIO

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    _, height = A4
    y = height - 60
    c.setFont("Helvetica", 11)
    for line in lines:
        c.drawString(50, y, line)
        y -= 18
        if y < 60:
            c.showPage()
            c.setFont("Helvetica", 11)
            y = height - 60
    c.save()
    return buf.getvalue()


def main() -> None:
    if CASES.exists():
        shutil.rmtree(CASES)
    SAMPLES.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- case 01
    p_case01 = write_case(
        "case01_email_it_clean", "input.eml",
        eml(
            "Acciaierie Rossi S.p.A.", "ordini@acciaierierossi.it",
            "Ordine di acquisto PO-2026-4501",
            "Spett.le Duferco Commerciale,\n\n"
            "con la presente confermiamo il nostro ordine PO-2026-4501:\n\n"
            "- 25 t tondo B450C 12mm a 614,40 €/t\n"
            "- 30 t tondo B450C 16mm a 609,60 €/t\n\n"
            "Consegna: 15/10/2026 presso ns. stabilimento di Brescia.\n\n"
            "Cordiali saluti,\nUfficio Acquisti\nAcciaierie Rossi S.p.A.\n",
        ),
        {
            "title": "Clean Italian order email, prices match agreements",
            "judge": {"focus": "fidelity of line extraction from Italian email prose"},
            "expected": {
                "customer_id": "C001", "order_ref": "PO-2026-4501", "n_lines": 2,
                "lines": [
                    {"sku": "TND-B450C-12", "quantity_t": 25.0, "unit_price": 614.40, "verdict": "approve"},
                    {"sku": "TND-B450C-16", "quantity_t": 30.0, "unit_price": 609.60, "verdict": "approve"},
                ],
                "order_verdict": "auto_approve",
                "absent_exception_codes": ["PRICE_MISMATCH", "UNKNOWN_PRODUCT"],
            },
        },
    )

    # ------------------------------------------------------------- case 02
    write_case(
        "case02_email_en_clean", "input.eml",
        eml(
            "SteelTrade GmbH", "purchasing@steeltrade.ch",
            "Purchase order PO-88410",
            "Dear Duferco team,\n\nplease book our order PO-88410:\n\n"
            "- 40 t hot rolled coils 3mm at 640.20 EUR/t\n"
            "- 30 t hot rolled coils 5mm at 659.60 EUR/t\n\n"
            "Delivery: 2026-10-20 to our Basel warehouse.\n\n"
            "Best regards,\nSteelTrade GmbH\n",
        ),
        {
            "title": "Clean English order email",
            "expected": {
                "customer_id": "C004", "order_ref": "PO-88410", "n_lines": 2,
                "lines": [
                    {"sku": "HRC-S235-3", "quantity_t": 40.0, "unit_price": 640.20, "verdict": "approve"},
                    {"sku": "HRC-S275-5", "quantity_t": 30.0, "unit_price": 659.60, "verdict": "approve"},
                ],
                "order_verdict": "auto_approve",
            },
        },
    )

    # ------------------------------------------------------------- case 03
    p_case03 = write_case(
        "case03_csv_clean", "input.csv",
        "Cliente: Costruzioni Ferrari S.p.A.\n"
        "Rif: CF-ORD-2211\n"
        "Codice;Descrizione;Qta;UM;Prezzo;Consegna\n"
        "TRV-HEB-200;Travi HEB 200 S275JR;45;t;712,50;20/10/2026\n"
        "TRV-IPE-160;Travi IPE 160 S235JR;30;t;688,75;20/10/2026\n"
        "RET-EL-6X20;Rete elettrosaldata maglia 20x20;12;t;655,50;20/10/2026\n",
        {
            "title": "Clean CSV with SKUs, code-path extraction",
            "expected": {
                "customer_id": "C003", "order_ref": "CF-ORD-2211", "n_lines": 3,
                "lines": [
                    {"sku": "TRV-HEB-200", "quantity_t": 45.0, "unit_price": 712.50, "verdict": "approve"},
                    {"sku": "TRV-IPE-160", "quantity_t": 30.0, "unit_price": 688.75, "verdict": "approve"},
                    {"sku": "RET-EL-6X20", "quantity_t": 12.0, "unit_price": 655.50, "verdict": "approve"},
                ],
                "order_verdict": "auto_approve",
            },
        },
    )

    # ------------------------------------------------------------- case 04
    xlsx04 = make_xlsx([
        ["Metallurgica Bianchi S.r.l."],
        ["P.IVA IT09876543210"],
        ["Ordine n. MB-1188"],
        [],
        ["Codice", "Descrizione", "Q.tà", "U.M.", "Prezzo", "Note"],
        ["VRG-65", "Vergella SAE1008 6.5mm", 50, "t", 570.38, ""],
        ["LAM-DC01-10", "Lamiera decapata DC01 1.0mm", 15, "t", 702.00, ""],
    ])
    p_case04 = write_case(
        "case04_xlsx_clean", "input.xlsx", xlsx04,
        {
            "title": "Clean Excel order with letterhead preamble",
            "expected": {
                "customer_id": "C002", "order_ref": "MB-1188", "n_lines": 2,
                "lines": [
                    {"sku": "VRG-65", "quantity_t": 50.0, "unit_price": 570.38, "verdict": "approve"},
                    {"sku": "LAM-DC01-10", "quantity_t": 15.0, "unit_price": 702.00, "verdict": "approve"},
                ],
                "order_verdict": "auto_approve",
            },
        },
    )

    # ------------------------------------------------------------- case 05
    pdf05 = make_pdf([
        "Acciaierie Rossi S.p.A.",
        "Via delle Fucine 12, 25100 Brescia",
        "P.IVA IT01234567890",
        "",
        "ORDINE DI ACQUISTO PO-2026-4622",
        "Data ordine: 01/09/2026",
        "",
        "Spett.le Duferco Commerciale S.p.A.",
        "",
        "- 60 t coils laminati a caldo S235 3mm a 633,60 EUR/t",
        "- 25 t lamiera da treno S355 8mm a 672,00 EUR/t",
        "",
        "Consegna: 30/10/2026",
        "Resa: franco ns. stabilimento",
    ])
    p_case05 = write_case(
        "case05_pdf_clean", "input.pdf", pdf05,
        {
            "title": "Text PDF order (no OCR needed)",
            "judge": {"focus": "line extraction from PDF layout text"},
            "expected": {
                "customer_id": "C001", "order_ref": "PO-2026-4622", "n_lines": 2,
                "lines": [
                    {"sku": "HRC-S235-3", "quantity_t": 60.0, "unit_price": 633.60, "verdict": "approve"},
                    {"sku": "LAM-S355-8", "quantity_t": 25.0, "unit_price": 672.00, "verdict": "approve"},
                ],
                "order_verdict": "auto_approve",
            },
        },
    )

    # ------------------------------------------------------------- case 06
    write_case(
        "case06_decimal_formats_csv", "input.csv",
        "Cliente: Metallurgica Bianchi S.r.l.\n"
        "Rif: MB-1201\n"
        "Codice;Descrizione;Qta;UM;Prezzo\n"
        "TBO-EN10219-60;Tubi saldati 60x60x3;18;t;726,38\n"
        "HRC-S235-3;Coils laminati a caldo 3mm;1.250;kg;643,50\n",
        {
            "title": "Italian decimal/thousands formats + kg line below MOQ",
            "expected": {
                "customer_id": "C002", "order_ref": "MB-1201", "n_lines": 2,
                "lines": [
                    {"sku": "TBO-EN10219-60", "quantity_t": 18.0, "unit_price": 726.38, "verdict": "approve"},
                    {"sku": "HRC-S235-3", "quantity_t": 1.25, "unit_price": 643.50, "verdict": "review"},
                ],
                "order_verdict": "needs_review",
                "exception_codes": ["BELOW_MOQ", "UNIT_CONVERTED"],
            },
        },
    )

    # ------------------------------------------------------------- case 07
    write_case(
        "case07_kg_conversion_txt", "input.txt",
        "Officine Marchetti S.n.c.\n"
        "P.IVA IT04443332221\n\n"
        "Ordine OM-341\n"
        "Data ordine: 01/09/2026\n\n"
        "- 40.000 kg rete elettrosaldata 6mm maglia 20x20 a 690,00 €/t\n"
        "- 16 t travi IPE 160 a 725,00 €/t\n\n"
        "Consegna: 10/10/2026\n\nCordiali saluti\n",
        {
            "title": "kg quantity converted to tonnes",
            "expected": {
                "customer_id": "C006", "order_ref": "OM-341", "n_lines": 2,
                "lines": [
                    {"sku": "RET-EL-6X20", "quantity_t": 40.0, "unit_price": 690.00, "verdict": "approve"},
                    {"sku": "TRV-IPE-160", "quantity_t": 16.0, "unit_price": 725.00, "verdict": "approve"},
                ],
                "order_verdict": "auto_approve",
                "exception_codes": ["UNIT_CONVERTED"],
            },
        },
    )

    # ------------------------------------------------------------- case 08
    write_case(
        "case08_unknown_product_csv", "input.csv",
        "Cliente: Costruzioni Ferrari S.p.A.\n"
        "Rif: CF-ORD-2299\n"
        "Codice;Descrizione;Qta;UM;Prezzo\n"
        "TRV-HEB-200;Travi HEB 200;20;t;712,50\n"
        "XXX-99;Materiale speciale fuori listino;10;t;500,00\n",
        {
            "title": "Unknown product code rejected, order to review",
            "expected": {
                "customer_id": "C003", "order_ref": "CF-ORD-2299", "n_lines": 2,
                "lines": [
                    {"sku": "TRV-HEB-200", "quantity_t": 20.0, "unit_price": 712.50, "verdict": "approve"},
                    {"sku": None, "quantity_t": 10.0, "unit_price": 500.00, "verdict": "reject"},
                ],
                "order_verdict": "needs_review",
                "exception_codes": ["UNKNOWN_PRODUCT"],
            },
        },
    )

    # ------------------------------------------------------------- case 09
    write_case(
        "case09_fuzzy_descriptions_txt", "input.txt",
        "Prefabbricati Esposito S.p.A.\n"
        "P.IVA IT07778889990\n\n"
        "Ordine PE-2026-77\n"
        "- 22 t rete elettrosaldata 6mm maglia 20x20 a 676,20 €/t\n"
        "- 14 t lamiera decapata DC01 1.0mm a 705,60 €/t\n\n"
        "Consegna: 12/10/2026\n",
        {
            "title": "No SKUs: fuzzy match on descriptions only",
            "expected": {
                "customer_id": "C008", "order_ref": "PE-2026-77", "n_lines": 2,
                "lines": [
                    {"sku": "RET-EL-6X20", "quantity_t": 22.0, "unit_price": 676.20, "verdict": "approve"},
                    {"sku": "LAM-DC01-10", "quantity_t": 14.0, "unit_price": 705.60, "verdict": "approve"},
                ],
                "order_verdict": "auto_approve",
            },
        },
    )

    # ------------------------------------------------------------- case 10
    write_case(
        "case10_price_blocking_csv", "input.csv",
        "Cliente: Acciaierie Rossi S.p.A.\n"
        "Rif: AR-5533\n"
        "Codice;Descrizione;Qta;UM;Prezzo\n"
        "TND-B450C-12;Tondo B450C 12mm;30;t;676,00\n"
        "VRG-65;Vergella 6.5mm;30;t;561,60\n",
        {
            "title": "Price +10% vs agreement: line rejected",
            "expected": {
                "customer_id": "C001", "order_ref": "AR-5533", "n_lines": 2,
                "lines": [
                    {"sku": "TND-B450C-12", "quantity_t": 30.0, "unit_price": 676.00, "verdict": "reject"},
                    {"sku": "VRG-65", "quantity_t": 30.0, "unit_price": 561.60, "verdict": "approve"},
                ],
                "order_verdict": "needs_review",
                "exception_codes": ["PRICE_MISMATCH"],
            },
        },
    )

    # ------------------------------------------------------------- case 11
    write_case(
        "case11_price_review_csv", "input.csv",
        "Cliente: Acciaierie Rossi S.p.A.\n"
        "Rif: AR-5560\n"
        "Codice;Descrizione;Qta;UM;Prezzo\n"
        "TND-B450C-16;Tondo B450C 16mm;25;t;629,00\n",
        {
            "title": "Price +3.2%: within block threshold, needs review",
            "expected": {
                "customer_id": "C001", "order_ref": "AR-5560", "n_lines": 1,
                "lines": [
                    {"sku": "TND-B450C-16", "quantity_t": 25.0, "unit_price": 629.00, "verdict": "review"},
                ],
                "order_verdict": "needs_review",
                "exception_codes": ["PRICE_MISMATCH"],
            },
        },
    )

    # ------------------------------------------------------------- case 12
    xlsx12 = make_xlsx([
        ["Costruzioni Ferrari S.p.A."],
        ["Ordine n. CF-ORD-2310"],
        ["Codice", "Descrizione", "Qta", "UM", "Prezzo"],
        ["TRV-HEB-200", "Travi HEB 200", 8, "t", 712.50],
        ["PRF-UPN-120", "Profilati UPN 120", 20, "t", 679.25],
    ])
    write_case(
        "case12_below_moq_xlsx", "input.xlsx", xlsx12,
        {
            "title": "Quantity below minimum order quantity",
            "expected": {
                "customer_id": "C003", "order_ref": "CF-ORD-2310", "n_lines": 2,
                "lines": [
                    {"sku": "TRV-HEB-200", "quantity_t": 8.0, "unit_price": 712.50, "verdict": "review"},
                    {"sku": "PRF-UPN-120", "quantity_t": 20.0, "unit_price": 679.25, "verdict": "approve"},
                ],
                "order_verdict": "needs_review",
                "exception_codes": ["BELOW_MOQ"],
            },
        },
    )

    # ------------------------------------------------------------- case 13
    write_case(
        "case13_over_credit_txt", "input.txt",
        "Officine Marchetti S.n.c.\n"
        "Ordine OM-355\n"
        "- 120 t travi HEB 200 a 750,00 €/t\n"
        "Consegna: 20/10/2026\n",
        {
            "title": "Order value exceeds customer credit limit",
            "expected": {
                "customer_id": "C006", "order_ref": "OM-355", "n_lines": 1,
                "lines": [
                    {"sku": "TRV-HEB-200", "quantity_t": 120.0, "unit_price": 750.00, "verdict": "approve"},
                ],
                "order_verdict": "needs_review",
                "exception_codes": ["OVER_CREDIT_LIMIT"],
            },
        },
    )

    # ------------------------------------------------------------- case 14
    write_case(
        "case14_blocked_customer_txt", "input.txt",
        "Edilizia Colombo S.r.l.\n"
        "P.IVA IT05556667778\n\n"
        "Ordine EC-889\n"
        "- 25 t tondo B450C 12mm a 627,20 €/t\n"
        "Consegna: 15/10/2026\n",
        {
            "title": "Blocked customer: order rejected before ERP",
            "expected": {
                "customer_id": "C005", "order_ref": "EC-889", "n_lines": 1,
                "lines": [
                    {"sku": "TND-B450C-12", "quantity_t": 25.0, "unit_price": 627.20, "verdict": "approve"},
                ],
                "order_verdict": "rejected",
                "exception_codes": ["CUSTOMER_BLOCKED"],
            },
        },
    )

    # ------------------------------------------------------------- case 15
    write_case(
        "case15_duplicate_ref_csv", "input.csv",
        "Cliente: Metallurgica Bianchi S.r.l.\n"
        "Rif: MB-1150\n"
        "Codice;Descrizione;Qta;UM;Prezzo\n"
        "TND-B450C-12;Tondo B450C 12mm;25;t;624,00\n",
        {
            "title": "Order reference already present in the ERP",
            "setup": {"erp_orders": [{"customer_id": "C002", "order_ref": "MB-1150"}]},
            "expected": {
                "customer_id": "C002", "order_ref": "MB-1150", "n_lines": 1,
                "lines": [
                    {"sku": "TND-B450C-12", "quantity_t": 25.0, "unit_price": 624.00, "verdict": "approve"},
                ],
                "order_verdict": "needs_review",
                "exception_codes": ["DUPLICATE_ORDER_REF"],
            },
        },
    )

    # ------------------------------------------------------------- case 16
    write_case(
        "case16_past_delivery_csv", "input.csv",
        "Customer: SteelTrade GmbH\n"
        "Order ref: ST-2026-118\n"
        "Codice;Descrizione;Qta;UM;Prezzo;Consegna\n"
        "HRC-S235-3;Hot rolled coils 3mm;40;t;640,20;15/08/2026\n",
        {
            "title": "Delivery date in the past",
            "expected": {
                "customer_id": "C004", "order_ref": "ST-2026-118", "n_lines": 1,
                "lines": [
                    {"sku": "HRC-S235-3", "quantity_t": 40.0, "unit_price": 640.20, "verdict": "review"},
                ],
                "order_verdict": "needs_review",
                "exception_codes": ["R5_DELIVERY_DATE"],
            },
        },
    )

    # ------------------------------------------------------------- case 17
    write_case(
        "case17_multiline_eml", "input.eml",
        eml(
            "Costruzioni Ferrari S.p.A.", "acquisti@costruzioniferrari.it",
            "Ordine CF-ORD-2415",
            "Buongiorno,\n\nordine CF-ORD-2415 come segue:\n\n"
            "- 45 t travi HEB 200 a 712,50 €/t\n"
            "- 30 t travi IPE 160 a 688,75 €/t\n"
            "- 25 t tondo B450C 12mm a 608,00 €/t\n"
            "- 25 t tondo B450C 16mm a 603,25 €/t\n"
            "- 10 t profilati UPN 120 a 679,25 €/t\n"
            "- 30 t coils laminati a caldo S275 5mm a 646,00 €/t\n\n"
            "Consegna: 25/10/2026\n\nCordiali saluti\n",
        ),
        {
            "title": "Six-line order, one line below MOQ",
            "expected": {
                "customer_id": "C003", "order_ref": "CF-ORD-2415", "n_lines": 6,
                "lines": [
                    {"sku": "TRV-HEB-200", "quantity_t": 45.0, "unit_price": 712.50, "verdict": "approve"},
                    {"sku": "TRV-IPE-160", "quantity_t": 30.0, "unit_price": 688.75, "verdict": "approve"},
                    {"sku": "TND-B450C-12", "quantity_t": 25.0, "unit_price": 608.00, "verdict": "approve"},
                    {"sku": "TND-B450C-16", "quantity_t": 25.0, "unit_price": 603.25, "verdict": "approve"},
                    {"sku": "PRF-UPN-120", "quantity_t": 10.0, "unit_price": 679.25, "verdict": "review"},
                    {"sku": "HRC-S275-5", "quantity_t": 30.0, "unit_price": 646.00, "verdict": "approve"},
                ],
                "order_verdict": "needs_review",
                "exception_codes": ["BELOW_MOQ"],
            },
        },
    )

    # ------------------------------------------------------------- case 18
    xlsx18 = make_xlsx([
        ["Metallurgica Bianchi S.r.l."],
        ["Ordine n. MB-1240"],
        ["Codice", "Descrizione", "Qta", "UM", "Prezzo"],
        ["VRG-65", "Vergella 6.5mm", None, "t", 570.38],
        ["LAM-DC01-10", "Lamiera decapata DC01", 12, "t", 702.00],
    ])
    write_case(
        "case18_missing_quantity_xlsx", "input.xlsx", xlsx18,
        {
            "title": "Missing quantity on one line",
            "expected": {
                "customer_id": "C002", "order_ref": "MB-1240", "n_lines": 2,
                "lines": [
                    {"sku": "VRG-65", "quantity_t": None, "unit_price": 570.38, "verdict": "reject"},
                    {"sku": "LAM-DC01-10", "quantity_t": 12.0, "unit_price": 702.00, "verdict": "approve"},
                ],
                "order_verdict": "needs_review",
                "exception_codes": ["MISSING_QUANTITY"],
            },
        },
    )

    # ------------------------------------------------------------- case 19
    p_case19 = write_case(
        "case19_irregular_note_txt", "input.txt",
        "Acciaierie Rossi S.p.A.\n"
        "Ordine AR-5601\n"
        "- 25 t tondo B450C 12mm a 614,40 €/t, consegna urgente se possibile entro fine settembre\n"
        "Consegna: 30/09/2026\n",
        {
            "title": "Free-text remark on a line forces human review",
            "judge": {"focus": "was the urgency remark preserved and attached to the line?"},
            "expected": {
                "customer_id": "C001", "order_ref": "AR-5601", "n_lines": 1,
                "lines": [
                    {"sku": "TND-B450C-12", "quantity_t": 25.0, "unit_price": 614.40, "verdict": "review"},
                ],
                "order_verdict": "needs_review",
                "exception_codes": ["R6_NOTES_REGULAR"],
            },
        },
    )

    # ------------------------------------------------------------- case 20
    write_case(
        "case20_unknown_customer_csv", "input.csv",
        "Cliente: Carpenterie Verdi S.r.l.\n"
        "Rif: CV-100\n"
        "Codice;Descrizione;Qta;UM;Prezzo\n"
        "TND-B450C-12;Tondo B450C 12mm;25;t;640,00\n",
        {
            "title": "Unknown customer: order rejected",
            "expected": {
                "customer_id": None, "order_ref": "CV-100", "n_lines": 1,
                "order_verdict": "rejected",
                "exception_codes": ["UNKNOWN_CUSTOMER"],
            },
        },
    )

    # ------------------------------------------------------------- case 21
    write_case(
        "case21_email_noise_eml", "input.eml",
        eml(
            "SteelTrade GmbH", "purchasing@steeltrade.ch",
            "RE: Conferma disponibilità - order PO-88521",
            "Hello,\n\nfurther to the call, please register order PO-88521:\n\n"
            "- 35 t heavy plate 8mm at 679.00 EUR/t\n\n"
            "Delivery: 2026-10-28\n\n"
            "Best regards,\nMarco Keller\nSteelTrade GmbH\nBaslerstrasse 100, CH-4052 Basel\n\n"
            "> On 28 Aug 2026 Duferco wrote:\n"
            "> we confirm availability of heavy plate 8mm\n"
            "> price as per frame agreement\n",
        ),
        {
            "title": "Reply thread with signature and quoted history",
            "judge": {"focus": "did the extraction ignore the quoted history and signature?"},
            "expected": {
                "customer_id": "C004", "order_ref": "PO-88521", "n_lines": 1,
                "lines": [
                    {"sku": "LAM-S355-8", "quantity_t": 35.0, "unit_price": 679.00, "verdict": "approve"},
                ],
                "order_verdict": "auto_approve",
            },
        },
    )

    # ------------------------------------------------------------- case 22
    write_case(
        "case22_all_unknown_products_txt", "input.txt",
        "BauStahl AG\n"
        "Bestellung BS-7701\n"
        "- 20 t Aluminiumbleche 2mm zu 2100,00 €/t\n"
        "- 5 t Kupferrohre 15mm zu 8900,00 €/t\n"
        "Lieferung: 15.10.2026\n",
        {
            "title": "No line matches the catalogue: order rejected",
            "expected": {
                "customer_id": "C007", "n_lines": 2,
                "lines": [
                    {"sku": None, "verdict": "reject"},
                    {"sku": None, "verdict": "reject"},
                ],
                "order_verdict": "rejected",
                "exception_codes": ["UNKNOWN_PRODUCT"],
            },
        },
    )

    # ------------------------------------------------------------- case 23
    write_case(
        "case23_no_prices_txt", "input.txt",
        "Prefabbricati Esposito S.p.A.\n"
        "Ordine PE-2026-90\n"
        "- 30 t tondo B450C 16mm\n"
        "- 12 t lamiera decapata DC01 1.0mm\n"
        "Consegna: 18/10/2026\n",
        {
            "title": "No prices in the order: agreed prices apply",
            "expected": {
                "customer_id": "C008", "order_ref": "PE-2026-90", "n_lines": 2,
                "lines": [
                    {"sku": "TND-B450C-16", "quantity_t": 30.0, "unit_price": None, "verdict": "approve"},
                    {"sku": "LAM-DC01-10", "quantity_t": 12.0, "unit_price": None, "verdict": "approve"},
                ],
                "order_verdict": "auto_approve",
                "exception_codes": ["MISSING_PRICE"],
                "absent_exception_codes": ["PRICE_MISMATCH"],
            },
        },
    )

    # ------------------------------------------------------------- case 24
    pdf24 = make_pdf([
        "BauStahl AG",
        "Hauptstrasse 15, 4133 Pratteln",
        "VAT CHE987654321",
        "",
        "Order no. BS-7720",
        "Date: 01/09/2026",
        "",
        "To: Duferco Commercial SA",
        "",
        "- 40 t heavy plate 8mm at 675.50 EUR/t",
        "- 28 t hot rolled coils 3mm at 636.90 EUR/t",
        "",
        "Delivery: 2026-10-22",
    ])
    write_case(
        "case24_pdf_swiss_en", "input.pdf", pdf24,
        {
            "title": "Swiss customer PDF in English, VAT lookup",
            "expected": {
                "customer_id": "C007", "order_ref": "BS-7720", "n_lines": 2,
                "lines": [
                    {"sku": "LAM-S355-8", "quantity_t": 40.0, "unit_price": 675.50, "verdict": "approve"},
                    {"sku": "HRC-S235-3", "quantity_t": 28.0, "unit_price": 636.90, "verdict": "approve"},
                ],
                "order_verdict": "auto_approve",
            },
        },
    )

    # ------------------------------------------------------------- case 25
    write_case(
        "case25_prose_email_llm", "input.eml",
        eml(
            "Acciaierie Rossi S.p.A.", "ordini@acciaierierossi.it",
            "ordine come da telefonata",
            "Buongiorno Franco,\n\n"
            "come anticipato al telefono vi confermiamo l'ordine: ci servono venticinque\n"
            "tonnellate del solito tondo da 12 come l'ultima fornitura di luglio, stesso\n"
            "prezzo, e trenta tonnellate del tondo da 16 se riuscite a consegnarle entro\n"
            "metà ottobre.\n\n"
            "Riferimento interno AR-5620.\n\nGrazie mille,\nPaola\n",
        ),
        {
            "title": "Prose order with numbers in words - needs the LLM",
            "requires_llm": True,
            "judge": {"focus": "quantities in words, vague references to past supplies"},
            "expected": {
                "customer_id": "C001", "n_lines": 2,
                "lines": [
                    {"sku": "TND-B450C-12", "quantity_t": 25.0, "unit_price": None, "verdict": "review"},
                    {"sku": "TND-B450C-16", "quantity_t": 30.0, "unit_price": None, "verdict": "review"},
                ],
                "order_verdict": "needs_review",
            },
        },
    )

    # --------------------------------------------------------------- samples
    shutil.copy(p_case01, SAMPLES / "ordine_email_acciaierie_rossi.eml")
    shutil.copy(p_case03, SAMPLES / "ordine_csv_costruzioni_ferrari.csv")
    shutil.copy(p_case04, SAMPLES / "ordine_excel_metallurgica_bianchi.xlsx")
    shutil.copy(p_case05, SAMPLES / "ordine_pdf_acciaierie_rossi.pdf")
    shutil.copy(p_case19, SAMPLES / "ordine_nota_urgente.txt")

    n_cases = len(list(CASES.iterdir()))
    print(f"Generated {n_cases} eval cases in {CASES}")
    print(f"Samples in {SAMPLES}: {sorted(p.name for p in SAMPLES.iterdir())}")


if __name__ == "__main__":
    main()
