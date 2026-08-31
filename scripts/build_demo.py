"""Build the static demo (app/netlify/) for Netlify.

Runs the real Python pipeline on the bundled samples (deterministic mode,
in-memory ERP) and freezes each run as JSON. The static page replays those
runs and processes uploaded .txt/.csv/.eml files with its own small
deterministic JS engine. Re-run after changing samples or the UI:

    python scripts/build_demo.py
"""

from __future__ import annotations

import contextlib
import csv
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "app" / "netlify"


def main() -> None:
    import os

    os.environ["ORDERFLOW_LLM"] = "stub"  # frozen runs must be deterministic
    from order_workflow.config import load_config
    from order_workflow.erp import MockERP
    from order_workflow.pipeline import Pipeline

    config = load_config()
    pipeline = Pipeline(config, erp=MockERP(":memory:"), trace=False)

    samples = sorted(
        p for p in config.samples_dir.iterdir()
        if p.suffix.lower() in {".eml", ".pdf", ".xlsx", ".csv", ".txt"}
    )
    runs = {}
    for path in samples:
        run = pipeline.process(path)
        run.source_file = path.name
        runs[path.name] = run.model_dump(mode="json")
        print(f"  {path.name}: {run.checked.summary if run.checked else run.error}")

    def read_csv(name: str) -> list[dict]:
        with open(config.reference_dir / name, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        for row in rows:
            for key, value in row.items():
                with contextlib.suppress(TypeError, ValueError):
                    row[key] = float(value)
        return rows

    data = {
        "reference": {
            "customers": read_csv("customers.csv"),
            "products": read_csv("products.csv"),
            "config": {
                "price_review_tolerance_pct": config.price_review_tolerance_pct,
                "price_block_tolerance_pct": config.price_block_tolerance_pct,
                "min_lead_days": config.min_lead_days,
            },
        },
        "sample_meta": [{"name": p.name, "size": p.stat().st_size} for p in samples],
        "samples": runs,
    }

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "demo_data.js").write_text(
        "window.DEMO_DATA = " + json.dumps(data, ensure_ascii=False) + ";\n",
        encoding="utf-8",
    )
    shutil.copy(ROOT / "src" / "order_workflow" / "webstatic" / "index.html", OUT / "index.html")
    size = (OUT / "demo_data.js").stat().st_size
    print(f"Static demo built in {OUT} (demo_data.js: {size / 1024:.0f} KiB)")


if __name__ == "__main__":
    main()
