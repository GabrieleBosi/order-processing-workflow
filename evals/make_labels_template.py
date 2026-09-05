"""Regenerate evals/labels.jsonl from the cases that carry a `judge` block.

    python -m evals.make_labels_template            # refuses to clobber labels
    python -m evals.make_labels_template --force    # rewrite, losing hand labels

Existing `human_score` and `note` values are carried over by case id, so
adding a case to the suite does not throw away work already done.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import tracking

LABELS_PATH = tracking.REPO_ROOT / "evals" / "labels.jsonl"


def build(cases_dir: Path, existing: dict[str, dict]) -> list[dict]:
    rows = []
    for case_dir in sorted(d for d in cases_dir.iterdir() if (d / "case.json").exists()):
        spec = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
        if "judge" not in spec:
            continue
        prior = existing.get(case_dir.name, {})
        rows.append({
            "case": case_dir.name,
            "category": spec.get("category", "uncategorised"),
            "focus": spec["judge"].get("focus", "overall fidelity"),
            "human_score": prior.get("human_score"),
            "note": prior.get("note", ""),
        })
    return rows


def read_existing(path: Path) -> dict[str, dict]:
    if not path.is_file():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        row = json.loads(line)
        out[row["case"]] = row
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.make_labels_template", description=__doc__)
    parser.add_argument("--cases", type=Path, default=tracking.CASES_DIR)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    existing = read_existing(LABELS_PATH)
    labelled = sum(1 for r in existing.values() if r.get("human_score") is not None)
    if labelled and not args.force:
        print(f"{LABELS_PATH} already holds {labelled} hand label(s). "
              "Re-run with --force if you really want to rewrite it.")
        return 1

    rows = build(args.cases, existing)
    LABELS_PATH.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
    )
    print(f"Wrote {len(rows)} label rows to {LABELS_PATH} "
          f"({labelled} existing score(s) preserved).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
