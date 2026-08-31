"""Mock ERP backed by SQLite.

Stands in for the real ERP write API. Step 5 only ever goes through this
module, and only after explicit human confirmation - the guardrail lives
in the pipeline, the ERP itself is plain code.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .models import CheckedOrder, ERPWriteResult, LineVerdict

SCHEMA = """
CREATE TABLE IF NOT EXISTS sales_orders (
    erp_order_id TEXT PRIMARY KEY,
    order_ref TEXT,
    customer_id TEXT,
    customer_name TEXT,
    currency TEXT,
    total_value REAL,
    status TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS sales_order_lines (
    erp_order_id TEXT,
    line_no INTEGER,
    sku TEXT,
    description TEXT,
    quantity_t REAL,
    unit_price REAL,
    line_value REAL,
    delivery_date TEXT,
    verdict TEXT,
    PRIMARY KEY (erp_order_id, line_no)
);
"""


class MockERP:
    def __init__(self, db_path: Path | str):
        self.db_path = str(db_path)
        self.db = sqlite3.connect(self.db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------- queries

    def order_ref_exists(self, customer_id: str, order_ref: str) -> bool:
        if not order_ref:
            return False
        row = self.db.execute(
            "SELECT 1 FROM sales_orders WHERE customer_id = ? AND UPPER(order_ref) = ?",
            (customer_id, order_ref.upper()),
        ).fetchone()
        return row is not None

    def list_orders(self, limit: int = 50) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM sales_orders ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        orders = []
        for row in rows:
            lines = self.db.execute(
                "SELECT * FROM sales_order_lines WHERE erp_order_id = ? ORDER BY line_no",
                (row["erp_order_id"],),
            ).fetchall()
            orders.append({**dict(row), "lines": [dict(line) for line in lines]})
        return orders

    # -------------------------------------------------------------- writes

    def _next_order_id(self) -> str:
        year = datetime.now(UTC).year
        row = self.db.execute(
            "SELECT COUNT(*) AS n FROM sales_orders WHERE erp_order_id LIKE ?", (f"SO-{year}-%",)
        ).fetchone()
        return f"SO-{year}-{row['n'] + 1:05d}"

    def write_order(self, checked: CheckedOrder) -> ERPWriteResult:
        """Write approved/review lines of a confirmed order; skip rejected ones.

        Human confirmation is enforced upstream (Pipeline.confirm); by the
        time we get here the reviewer has seen every flag.
        """
        writable = [line for line in checked.lines if line.verdict != LineVerdict.REJECT]
        skipped = [
            line.reconciled.extracted.line_no
            for line in checked.lines
            if line.verdict == LineVerdict.REJECT
        ]
        if not writable:
            return ERPWriteResult(
                erp_order_id=None, written_lines=0, skipped_line_nos=skipped,
                message="No writable lines: every line was rejected.",
            )
        customer = checked.reconciled.customer
        extracted = checked.reconciled.extracted
        erp_id = self._next_order_id()
        total = round(sum(line.reconciled.line_value or 0.0 for line in writable), 2)
        self.db.execute(
            "INSERT INTO sales_orders VALUES (?,?,?,?,?,?,?,?)",
            (
                erp_id,
                extracted.order_ref or "",
                customer.customer_id if customer else "",
                customer.name if customer else (extracted.customer_name or ""),
                extracted.currency,
                total,
                "open",
                datetime.now(UTC).isoformat(timespec="seconds"),
            ),
        )
        for line in writable:
            rec = line.reconciled
            ext = rec.extracted
            self.db.execute(
                "INSERT INTO sales_order_lines VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    erp_id,
                    ext.line_no,
                    rec.product.sku if rec.product else (ext.sku or ""),
                    rec.product.name if rec.product else ext.description,
                    rec.quantity_t,
                    ext.unit_price,
                    rec.line_value,
                    (ext.delivery_date or extracted.delivery_date).isoformat()
                    if (ext.delivery_date or extracted.delivery_date) else None,
                    line.verdict.value,
                ),
            )
        self.db.commit()
        return ERPWriteResult(
            erp_order_id=erp_id,
            written_lines=len(writable),
            skipped_line_nos=skipped,
            message=f"Order {erp_id} written with {len(writable)} line(s).",
        )
