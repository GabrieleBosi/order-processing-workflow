"""Reference master data (customers, products, price list) behind SQL queries.

Step 3 (reconcile) is deliberately query-based: matching an order line to
master data is exact, cheap and testable, so no model is involved. CSVs are
loaded into an in-memory SQLite database and every lookup is a real query.
"""

from __future__ import annotations

import csv
import difflib
import re
import sqlite3
from pathlib import Path

from .models import MatchedCustomer, MatchedProduct


def _norm(s: str) -> str:
    """Normalize free text for matching: lowercase, collapse punctuation."""
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


class ReferenceData:
    def __init__(self, reference_dir: Path):
        self.reference_dir = Path(reference_dir)
        # check_same_thread=False: the web app serves requests from a thread
        # pool; access is read-only after loading.
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._load()

    # ---------------------------------------------------------------- load

    def _load(self) -> None:
        cur = self.db.cursor()
        cur.executescript(
            """
            CREATE TABLE customers (
                customer_id TEXT PRIMARY KEY, name TEXT, vat_number TEXT,
                country TEXT, payment_terms TEXT, credit_limit_eur REAL,
                status TEXT, discount_pct REAL
            );
            CREATE TABLE products (
                sku TEXT PRIMARY KEY, name TEXT, name_en TEXT, aliases TEXT,
                unit TEXT, min_order_qty REAL, list_price_eur REAL
            );
            """
        )
        with open(self.reference_dir / "customers.csv", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                cur.execute(
                    "INSERT INTO customers VALUES (?,?,?,?,?,?,?,?)",
                    (
                        row["customer_id"], row["name"], row["vat_number"], row["country"],
                        row["payment_terms"], float(row["credit_limit_eur"]), row["status"],
                        float(row["discount_pct"]),
                    ),
                )
        with open(self.reference_dir / "products.csv", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                cur.execute(
                    "INSERT INTO products VALUES (?,?,?,?,?,?,?)",
                    (
                        row["sku"], row["name"], row["name_en"], row["aliases"], row["unit"],
                        float(row["min_order_qty"]), float(row["list_price_eur"]),
                    ),
                )
        self.db.commit()

    # ------------------------------------------------------------ customers

    def find_customer(self, name: str | None, vat: str | None = None) -> MatchedCustomer | None:
        cur = self.db.cursor()
        if vat:
            row = cur.execute(
                "SELECT * FROM customers WHERE REPLACE(UPPER(vat_number),' ','') = ?",
                (vat.upper().replace(" ", ""),),
            ).fetchone()
            if row:
                return self._customer(row)
        if not name:
            return None
        target = _norm(name)
        rows = cur.execute("SELECT * FROM customers").fetchall()
        best, best_score = None, 0.0
        for row in rows:
            cand = _norm(row["name"])
            score = difflib.SequenceMatcher(None, target, cand).ratio()
            # A customer name contained in the other counts as a strong match
            # ("Acciaierie Rossi" vs "Acciaierie Rossi S.p.A.").
            if cand in target or target in cand:
                score = max(score, 0.93)
            if score > best_score:
                best, best_score = row, score
        if best is not None and best_score >= 0.75:
            return self._customer(best)
        return None

    @staticmethod
    def _customer(row: sqlite3.Row) -> MatchedCustomer:
        return MatchedCustomer(
            customer_id=row["customer_id"],
            name=row["name"],
            status=row["status"],
            credit_limit_eur=row["credit_limit_eur"],
            discount_pct=row["discount_pct"],
            payment_terms=row["payment_terms"],
        )

    # ------------------------------------------------------------- products

    def find_product_by_sku(self, sku: str) -> tuple[MatchedProduct, str] | None:
        """Exact SKU or alias match. Returns (product, method)."""
        cur = self.db.cursor()
        key = sku.strip().upper()
        row = cur.execute("SELECT * FROM products WHERE UPPER(sku) = ?", (key,)).fetchone()
        if row:
            return self._product(row), "exact_sku"
        rows = cur.execute("SELECT * FROM products WHERE aliases <> ''").fetchall()
        for row in rows:
            aliases = {a.strip().upper() for a in row["aliases"].split("|") if a.strip()}
            if key in aliases:
                return self._product(row), "alias"
        return None

    def search_product_by_description(
        self, description: str
    ) -> tuple[MatchedProduct, float] | None:
        """Fuzzy match on product names/aliases. Returns (product, confidence)."""
        if not description:
            return None
        target = _norm(description)
        target_tokens = set(target.split())
        if not target_tokens:
            return None
        cur = self.db.cursor()
        best, best_score = None, 0.0
        for row in cur.execute("SELECT * FROM products").fetchall():
            candidates = [row["name"], row["name_en"]] + [
                a for a in row["aliases"].split("|") if a.strip()
            ]
            for cand in candidates:
                cn = _norm(cand)
                if not cn:
                    continue
                cand_tokens = set(cn.split())
                overlap = len(target_tokens & cand_tokens) / max(len(cand_tokens), 1)
                ratio = difflib.SequenceMatcher(None, target, cn).ratio()
                score = max(ratio, overlap * 0.9)
                if score > best_score:
                    best, best_score = row, score
        if best is not None and best_score >= 0.55:
            return self._product(best), round(best_score, 3)
        return None

    @staticmethod
    def _product(row: sqlite3.Row) -> MatchedProduct:
        return MatchedProduct(
            sku=row["sku"],
            name=row["name"],
            unit=row["unit"],
            min_order_qty=row["min_order_qty"],
            list_price=row["list_price_eur"],
        )

    def expected_price(self, customer: MatchedCustomer, product: MatchedProduct) -> float:
        """Agreed price = list price minus the customer's negotiated discount."""
        return round(product.list_price * (1 - customer.discount_pct / 100.0), 2)
