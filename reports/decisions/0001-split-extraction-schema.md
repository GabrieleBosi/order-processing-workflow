# 0001 - The extraction schema is split into two calls, not trimmed

Status: **applied** (session 5)
Date: 2026-09-05
Affects: `src/order_workflow/steps/extract.py`, `evals/run_mlflow.py` (cost estimate)

## The problem this fixes

The baseline run `ccb749688ded49d4bb4120e3af5ea7a3` failed 25 of 39 cases, and all 25
failed the same way: the extraction step never ran. `LLMClient.structured` sent the
`LLMOrder` model as the structured-output schema and the API refused it:

```
400 invalid_request_error: Schema is too complex.
```

Every optional field is an `anyOf` branch in the emitted JSON Schema, and the limit is on
the total, not on either half. `LLMOrder` carried 8 optional header fields plus an array
whose items carry 7 more. `reports/error_analysis.md` has the full attribution: this was
the biggest cell, 100% of the failures, and it was a blocker rather than a quality
problem - the same 39 cases scored 76.3% on the deterministic path.

## The measurement, repeated

Session 4 measured the boundary. It was re-measured here rather than inherited, with four
probe calls against `claude-opus-5` (tiny document, ~1 k input tokens each):

| schema | optionals | result |
|--------|----------:|--------|
| `LLMOrder` as shipped: header + lines | 8 + array | **REJECTED** `Schema is too complex.` |
| header alone, all 8 fields, flat | 8 | ACCEPTED (in 989 / out 113) |
| `{lines: list[LLMLine]}` alone | array | ACCEPTED (in 1075 / out 85) |
| 4 header optionals + lines | 4 + array | ACCEPTED (in 1454 / out 152) |

Both halves are accepted on their own. The two known-good shapes are therefore *4 + 8 in
one call* and *8 alone plus the array alone in two calls*, which is exactly the choice
below.

## The choice

The brief's tiebreaker is which option **keeps the extracted facts identical**.

**Route A - trim the header to 4 optionals (one call).** Four of the eight header fields
have to go. Only four are read downstream: `customer_name` and `customer_vat` (step 3
matches the customer on them), `order_ref` (an expected value in the cases, and the
duplicate-reference rule), `delivery_date` (the delivery-date rule in step 4). So the four
that would be dropped are `order_date`, `currency`, `language` and the order-level `notes`.
Nothing in the 39 cases' expected values names them - but `order_date` and `currency` are
commercial facts written on the documents, the judge scores fidelity to *the document*
("every commercial fact ... dates, references, remarks"), and the whole point of the
extraction step is to carry the document's facts forward. Route A loses four of them.

**Route B - two calls, same prompt, one schema each (chosen).** Ask for the header with
`LLMOrderHeader` (the 8 optionals, flat) and the lines with `LLMOrderLines` (the array).
Both shapes are measured-accepted. Every field that `ExtractedOrder` had before is still
populated from the model, so the extracted facts are identical to what the single call was
*designed* to produce - and, since the single call produced nothing at all, identical is
the most that can be asked.

Route B was chosen. The cost is one extra API call per irregular document, quantified
below; the benefit is that no downstream step, no case expectation and no judge criterion
has to be renegotiated because a field quietly disappeared.

## The prompt is unchanged

Both calls carry `EXTRACTION_SYSTEM` verbatim and the same user message that the single
call sent (`f"Extract the purchase order from this document:\n\n{_render_document(doc)}"`).
Only `output_model` differs between them. There is no wording diff to quote, and the run
parameters prove it rather than asserting it:

| param | baseline run | this session |
|-------|--------------|--------------|
| `prompt_text_sha256_extract` | `b766141f4bd6b109` | `b766141f4bd6b109` (unchanged) |
| `prompt_file_git_hash_extract` | `928485089bfccf09...` | `58f9e9009930e3a9...` (code changed) |

The text hash is over `EXTRACTION_SYSTEM` itself; the file hash moves because the schema
classes and `_extract_with_llm` live in the same file. That is the intended signature of a
schema-only change.

## What it costs

Per document without an order table: two calls instead of one, both carrying the whole
document, so step 2's input tokens double and its output grows by one small header object
(~113 tokens measured). The dry estimator in `evals/run_mlflow.py` was updated to count
both calls, because an estimate that hides the second call is worse than no estimate:

```
before   extract calls 25   pipeline $0.7266 + judge $0.0631 = $0.7896   (budget $1.00)
after    extract calls 50   pipeline $0.8383 + judge $0.0631 = $0.9014   (budget $1.00)
```

Still inside the 1.00 USD cap, with less headroom than before. Note the estimator is
deliberately pessimistic (it assumes every line needs a step-4 model opinion), but the
margin is now thin enough that a materially larger case set would push the guard over and
stop the run - which is the guard doing its job, and the point at which the header call
should be revisited.

Latency: step 2 now makes two sequential calls, so its per-case latency roughly doubles.
The suite's p95 threshold is re-instantiated from the new measured baseline, and it exists
to catch a hung call, not to police this.

## Falsification

The claim is that the four categories which read 100% deterministically were *blocked*
rather than weak. If it is right, the re-run restores them and the biggest cell in
`reports/error_analysis.md` moves off `pipeline`. If it is wrong, the extraction prompt is
the next suspect and the error analysis is re-run before touching it.

## One other change, forced by the re-run

The first re-run of the suite died 25 cases and 0.59 USD in, and it was not the schema:

```
UnicodeEncodeError: 'charmap' codec can't encode character '→'
```

`evals.py` prints each judge score and the first 100 characters of its rationale. The
judge for `case25_prose_email_llm` wrote an arrow into its rationale, this machine's
console encoding is cp1252, and `print` raised in the middle of the case loop. The
exception propagated out of `run_evals`, MLflow marked the run FAILED, and every metric of
those 25 cases was lost - the money was spent and nothing was measured.

`_make_console_lossless()` in `src/order_workflow/evals.py` now switches stdout and stderr
to `errors="replace"` at the start of a run. It is a reporting fix, not a change to the
system under test: no prompt, no schema, no rule, and the full text still reaches
`report.json` (written as UTF-8) and the MLflow trace unchanged. It is in this session
because without it a full run cannot complete on this platform, and the brief's third item
is a full run.

## What this deliberately does not do

Not fixing the French and German header maps, and not adding injection flagging. Both are
named in `reports/error_analysis.md` as the real findings, and they are the next two
sessions in that order. One fix, then measure.
