# Rubric: extraction fidelity

The only rubric the judge uses. It scores **one thing**: how faithfully the
extraction step reproduced the commercial facts of the source document. It
does **not** score whether the order is bookable — that is what the
deterministic rules in step 3 and step 4 are for, and they have objective
checks.

The judge never decides pass/fail. Its scores are logged next to the
objective results so error analysis can separate "the model read the document
wrong" from "the rules judged a correctly-read document differently than
expected". Until Cohen's kappa against human labels has been logged, the
judge is excluded from the gate entirely (see `evals/thresholds.yaml`).

## Which cases the judge scores

The 20 cases carrying a `judge` block in `case.json`. They are the ones whose
input is free text — email prose, plain-text notes, PDF letterheads — where
there is no single mechanical answer to "was this read correctly". Table
sources (CSV, Excel) are extracted by column mapping and are fully covered by
objective checks, so the judge does not look at them.

## The scale

The judge and the human labeller both answer the same question, on the same
1–5 scale, looking at the same two things: the source document, and the
`ExtractedOrder` JSON produced from it.

| Score | Meaning |
|-------|---------|
| 5 | Every commercial fact (products, quantities, prices, dates, references, remarks) is captured faithfully; nothing invented. |
| 4 | Complete on all order lines; minor loss in remarks or header fields. |
| 3 | Order lines correct but meaningful context (notes, dates) lost or garbled. |
| 2 | At least one order line wrong or missing. |
| 1 | Extraction unusable or contains invented facts. |

## Anchors, so two people score the same document the same way

These resolve the cases where the scale alone is ambiguous. They apply to the
human labeller and to the judge equally.

- **Invented facts are always 1**, however small. A price, date or reference
  that does not appear in the document and was not left null is a 1, even if
  every line is otherwise perfect. Silent completion is the failure mode this
  suite most needs to catch.
- **A missing or wrong line is at most 2.** Wrong means a quantity, unit or
  price that contradicts the document. Line *order* differing from the
  document is not wrong by itself.
- **A lost `order_ref`, `order_date` or `delivery_date` is a 4**, not a 3, as
  long as every line is right. Header fields are recoverable by a human in
  seconds; lines are not.
- **A lost free-text remark that changes commercial terms is a 3.** "Come
  l'ultima fornitura", "salvo conferma", "sconto da concordare" carry
  commercial meaning; dropping them hides something the human confirming the
  order needed to see. A dropped courtesy phrase is not a loss at all.
- **The customer being the addressee instead of the sender is a 2**, not a 1.
  It is a wrong fact rather than an invented one, and it is the single most
  common failure shape in letterhead documents.
- **Verbatim beats normalised.** A quantity written "25 t" and extracted as
  `25.0` with `unit: "t"` is a 5. The same quantity silently converted to
  25000 kg is a 3: correct arithmetic, lost fidelity, and step 3 is the place
  that is allowed to convert.
- **Judge only what the extraction claims.** A null field is not a loss when
  the document genuinely does not state the value; it is exactly right.

## The prompt text

The judge is given the block below verbatim, as its system prompt, together
with the case's `judge.focus` line. `tests/test_judge_rubric.py` asserts that
this block and `JUDGE_RUBRIC` in `src/order_workflow/evals.py` are identical,
so this file cannot drift from what the model is actually told.

```text
Score the extraction against the source document from 1 to 5:
5 = every commercial fact (products, quantities, prices, dates, references,
    remarks) is captured faithfully; nothing invented.
4 = complete on all order lines; minor loss in remarks or header fields.
3 = order lines correct but meaningful context (notes, dates) lost or garbled.
2 = at least one order line wrong or missing.
1 = extraction unusable or contains invented facts.
Judge only fidelity to the document, not whether the order is bookable.
```

## Known biases to watch

- **Self-preference.** The judge runs on a different model from the pipeline
  under test (`ORDERFLOW_JUDGE_MODEL`, default `claude-sonnet-5` against a
  `claude-opus-5` pipeline) precisely so it is not grading its own output.
- **Verbosity.** A longer `notes` field is not a more faithful one. The
  anchors above are about facts present or absent, never about length.
- **Position.** Only one extraction is shown per call, so there is no A/B
  ordering to be biased by. Do not batch two extractions into one judge call.
