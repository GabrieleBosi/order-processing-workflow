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
        if not (self.reference_dir / "customers.csv").is_file():
            raise FileNotFoundError(
                f"Reference data not found in {self.reference_dir}. Run from an editable "
                "install of the repository (pip install -e .) or point ORDERFLOW_DATA_DIR "
                "at a directory containing reference/customers.csv and reference/products.csv."
            )
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

    def find_customer(
        self, name: str | None, vat: str | None = None
    ) -> tuple[MatchedCustomer, float] | None:
        """Match a customer; returns (customer, confidence).

        VAT and exact/contained names are certain (1.0 / 0.93). A plain fuzzy
        hit keeps its ratio so step 3 can flag uncertain matches for review
        instead of silently booking to a similar-sounding company.
        """
        cur = self.db.cursor()
        if vat:
            row = cur.execute(
                "SELECT * FROM customers WHERE REPLACE(UPPER(vat_number),' ','') = ?",
                (vat.upper().replace(" ", ""),),
            ).fetchone()
            if row:
                return self._customer(row), 1.0
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
            return self._customer(best), round(best_score, 3)
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
    ) -> tuple[MatchedProduct, float, bool] | None:
        """Fuzzy match on product names/aliases.

        Returns (product, confidence, ambiguous). Two guards keep a
        wrong-but-similar product from being auto-approved:
        - digit groups must agree: "tondo 14mm" can never match "tondo 12mm";
        - a pure string-similarity hit (e.g. HEA 200 vs HEB 200) is capped at
          0.79 confidence, below step 4's 0.8 review gate - only an exact
          normalized name/alias or full token coverage scores higher.
        `ambiguous` is set when a different product scored almost as high.
        """
        if not description:
            return None
        target = _norm(description)
        target_tokens = set(target.split())
        if not target_tokens:
            return None
        target_digits = set(re.findall(r"\d+", target))
        cur = self.db.cursor()
        best, best_score = None, 0.0
        second_score, second_sku = 0.0, None
        for row in cur.execute("SELECT * FROM products").fetchall():
            candidates = [row["name"], row["name_en"]] + [
                a for a in row["aliases"].split("|") if a.strip()
            ]
            row_best = 0.0
            for cand in candidates:
                cn = _norm(cand)
                if not cn:
                    continue
                cand_digits = set(re.findall(r"\d+", cn))
                # Sizes/grades conflict when BOTH sides carry digits the other
                # lacks ("14mm" vs "12mm"); a candidate merely less specific
                # than the query (or vice versa) is fine.
                if (cand_digits - target_digits) and (target_digits - cand_digits):
                    continue
                if cn == target:
                    score = 1.0
                else:
                    cand_tokens = set(cn.split())
                    overlap = len(target_tokens & cand_tokens) / max(len(cand_tokens), 1)
                    ratio = difflib.SequenceMatcher(None, target, cn).ratio()
                    score = max(min(ratio, 0.79), overlap * 0.9)
                row_best = max(row_best, score)
            if row_best > best_score:
                if best is not None and best["sku"] != row["sku"]:
                    second_score, second_sku = best_score, best["sku"]
                best, best_score = row, row_best
            elif best is not None and row["sku"] != best["sku"] and row_best > second_score:
                second_score, second_sku = row_best, row["sku"]
        if best is not None and best_score >= 0.55:
            ambiguous = second_sku is not None and second_score >= max(0.55, best_score * 0.93)
            return self._product(best), round(best_score, 3), ambiguous
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
