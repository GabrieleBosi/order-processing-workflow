"""The eval suite is an engineering artifact (field-guide rule 24), so it is tested.

These tests never call an API. They check that the case set, the rubric, the
thresholds file and the gate stay internally consistent - the failure mode
they exist to catch is a suite that quietly stops measuring what it claims to.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from order_workflow.evals import COMPONENT_ORDER, JUDGE_RUBRIC, _category_scores

REPO_ROOT = Path(__file__).resolve().parent.parent
CASES = REPO_ROOT / "evals" / "cases"
RUBRIC = REPO_ROOT / "evals" / "rubrics" / "extraction_fidelity.md"
LABELS = REPO_ROOT / "evals" / "labels.jsonl"
THRESHOLDS = REPO_ROOT / "evals" / "thresholds.yaml"
THRESHOLDS_DET = REPO_ROOT / "evals" / "thresholds_deterministic.yaml"
ALL_THRESHOLDS = (THRESHOLDS, THRESHOLDS_DET)

KNOWN_CATEGORIES = {
    "clean", "parsing", "master_data", "business_rules", "multilingual", "safety",
}


def specs() -> dict[str, dict]:
    return {
        d.name: json.loads((d / "case.json").read_text(encoding="utf-8"))
        for d in sorted(CASES.iterdir())
        if (d / "case.json").exists()
    }


def test_every_case_declares_a_known_category_and_an_existing_input():
    for name, spec in specs().items():
        assert spec.get("category") in KNOWN_CATEGORIES, f"{name}: bad category"
        assert (CASES / name / spec["input"]).is_file(), f"{name}: input missing"


def test_the_suite_covers_the_categories_the_gate_reasons_about():
    counts: dict[str, int] = {}
    for spec in specs().values():
        counts[spec["category"]] = counts.get(spec["category"], 0) + 1
    assert set(counts) == KNOWN_CATEGORIES
    # The brief's floor: at least six multilingual and six safety cases.
    assert counts["multilingual"] >= 6
    assert counts["safety"] >= 6


def test_multilingual_covers_italian_french_and_german_twice_each():
    languages = {"it": 0, "fr": 0, "de": 0}
    for name, spec in specs().items():
        if spec["category"] != "multilingual":
            continue
        for lang in languages:
            if f"_{lang}_" in name:
                languages[lang] += 1
    assert all(n >= 2 for n in languages.values()), languages


def test_every_safety_case_expects_the_injection_ignored_and_flagged():
    for name, spec in specs().items():
        if spec["category"] != "safety":
            continue
        assert spec["expected"].get("injection_flagged") is True, f"{name}: not flag-checked"
        marker = spec.get("safety", {}).get("injection_marker", "")
        assert marker, f"{name}: no injection_marker for the harness to look for"
        source = (CASES / name / spec["input"]).read_bytes()
        # A PDF stores the text compressed, so only assert presence for the
        # formats where the marker is readable as plain bytes.
        if not spec["input"].endswith(".pdf"):
            assert marker.encode("utf-8") in source, f"{name}: marker not in the document"
        # Extraction only: the expected output is the genuine order, so every
        # safety case must still pin the real line count.
        assert "n_lines" in spec["expected"], f"{name}: no n_lines to pin the extraction"


def test_the_four_injected_intents_from_the_brief_are_all_covered():
    intents = {
        spec["safety"]["injected_intent"]
        for spec in specs().values()
        if spec["category"] == "safety"
    }
    assert intents == {
        "change the total", "add a line item", "skip the human confirmation", "write to the ERP"
    }


def test_the_rubric_file_and_the_prompt_the_judge_gets_cannot_drift():
    text = RUBRIC.read_text(encoding="utf-8")
    assert JUDGE_RUBRIC.strip() in text, (
        "evals/rubrics/extraction_fidelity.md no longer quotes JUDGE_RUBRIC verbatim"
    )


def test_the_label_template_covers_exactly_the_judge_cases():
    judge_cases = {name for name, spec in specs().items() if "judge" in spec}
    assert len(judge_cases) >= 20, f"only {len(judge_cases)} judge cases; the brief asks for 20"
    rows = [json.loads(ln) for ln in LABELS.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert {r["case"] for r in rows} == judge_cases
    for row in rows:
        assert row["human_score"] is None or 1 <= int(row["human_score"]) <= 5


def test_category_scores_exclude_skips_from_the_denominator():
    results = [
        {"category": "a", "status": "pass"},
        {"category": "a", "status": "fail"},
        {"category": "a", "status": "skip"},
    ]
    stats = _category_scores(results)["a"]
    assert stats == {
        "passed": 1, "failed": 1, "skipped": 1, "graded": 2, "total": 3, "pass_rate": 0.5
    }


def test_safety_is_last_in_the_component_order():
    # Error analysis charges a case to the first failing component, so a case
    # whose extraction was wrong must be charged to extract, not to safety.
    assert COMPONENT_ORDER[-1] == "safety"
    assert COMPONENT_ORDER.index("extract") < COMPONENT_ORDER.index("check")


# ------------------------------------------------------------------ gate

yaml = pytest.importorskip("yaml", reason="the gate needs the [mlflow] extra")


@pytest.mark.parametrize("path", ALL_THRESHOLDS, ids=lambda p: p.name)
def test_every_threshold_line_is_documented_and_well_formed(path):
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert cfg["metrics"], "no thresholds declared"
    for metric, spec in cfg["metrics"].items():
        assert spec["direction"] in ("higher_is_better", "lower_is_better"), metric
        assert isinstance(spec["threshold"], (int, float)), metric
        assert spec.get("why", "").strip(), f"{metric} has no `why`: every line needs its reason"


@pytest.mark.parametrize("path", ALL_THRESHOLDS, ids=lambda p: p.name)
def test_the_gate_has_a_threshold_for_every_category(path):
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    for category in KNOWN_CATEGORIES:
        assert f"pass_rate_{category}" in cfg["metrics"], f"no threshold for {category}"


def test_the_deterministic_gate_forbids_spending_anything():
    # The no-key configuration must reach no model at all. A cost above zero
    # means a code path called the API when it was meant to be on heuristics.
    cfg = yaml.safe_load(THRESHOLDS_DET.read_text(encoding="utf-8"))
    spec = cfg["metrics"]["cost_total_usd"]
    assert spec["direction"] == "lower_is_better"
    assert spec["threshold"] == 0.0


def test_the_judge_is_gated_behind_a_logged_kappa():
    from evals.gate import evaluate

    cfg = yaml.safe_load(THRESHOLDS.read_text(encoding="utf-8"))
    metrics = dict.fromkeys(cfg["metrics"], 1.0)
    metrics.update({"cost_total_usd": 0.0, "latency_p95_ms": 0.0})

    checks, notes = evaluate(cfg, metrics)
    assert not any(c.metric == "judge_kappa" for c in checks), "judge gated without a kappa"
    assert any("judge thresholds NOT enforced" in n for n in notes)

    checks, notes = evaluate(cfg, {**metrics, "judge_kappa": 0.9})
    assert any(c.metric == "judge_kappa" for c in checks), "kappa logged but never checked"
    assert not notes


def test_the_gate_fails_closed_on_a_metric_the_run_never_logged():
    from evals.gate import GateError, evaluate

    cfg = yaml.safe_load(THRESHOLDS.read_text(encoding="utf-8"))
    with pytest.raises(GateError, match="fails closed"):
        evaluate(cfg, {"pass_rate": 1.0})
