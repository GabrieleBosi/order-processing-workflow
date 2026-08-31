# LLM judge rubric

Objective checks cover everything that has a single right answer: line
counts, SKUs, quantities, prices, verdicts, exception codes. The judge is
used **only** where no objective check exists - the fidelity of free-text
extraction (remarks, vague references, urgency requests) - and it never
decides pass/fail; its scores are reported separately for error analysis.

The judge runs with `orderflow eval --judge` (needs an API key) and applies
this rubric per case, comparing the extraction against the source document:

| Score | Meaning |
|-------|---------|
| 5 | Every commercial fact (products, quantities, prices, dates, references, remarks) captured faithfully; nothing invented. |
| 4 | Complete on all order lines; minor loss in remarks or header fields. |
| 3 | Order lines correct but meaningful context (notes, dates) lost or garbled. |
| 2 | At least one order line wrong or missing. |
| 1 | Extraction unusable or contains invented facts. |

The judge is instructed to score fidelity to the document only - not
whether the order is bookable (that is what the deterministic checks and
step 4 are for).
