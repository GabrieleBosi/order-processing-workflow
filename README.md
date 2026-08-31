# Order-processing workflow

A working implementation of the order-intake workflow for a steel-trading
back office: customer purchase orders arrive as **email, PDF or Excel**, and
end - after deterministic checks and a human confirmation - as **rows in the
ERP** (mocked here with SQLite).

The workflow is a **fixed-step pipeline (Level 2 autonomy), not an agent**:
the sequence is stable, so every run takes the same five steps, which keeps
the process predictable, traceable and evaluable. The model is used only
where text is irregular; everything that can be code, is code - exact, free
and testable.

| # | Step | Executor | What it does |
|---|------|----------|--------------|
| 1 | **Normalize** | code (+ OCR fallback) | `.eml` / `.pdf` / `.xlsx` / `.csv` / `.txt` → one `NormalizedDocument` (text + tables). OCR only for scanned PDFs, and only if installed. |
| 2 | **Extract** | LLM *(code for regular input)* | Order lines + header (customer, reference, dates). Structured tables go through a deterministic column-mapping path; free text goes to the model (structured output validated by Pydantic). No key? A heuristic extractor stands in. |
| 3 | **Reconcile** | queries | SQL lookups against master data: customer (VAT > fuzzy name), product (SKU > alias > fuzzy), agreed price = list − customer discount, kg→t conversion, duplicate order ref against the ERP, credit limit. Exceptions are *signalled*, not judged. |
| 4 | **Check, line by line** | rules + LLM | The risky step. Hard rules first (price tolerance, MOQ, dates, mandatory fields); the model reads only irregular lines (free-text remarks, low-confidence matches) and **can only escalate** a verdict (approve→review→reject), never soften one. |
| 5 | **ERP write** | code | Behind an explicit **human confirmation**. Rejected orders can never be written; rejected lines are skipped and reported. |

## Design decisions

- **Code wherever possible.** Parsing numbers ("1.234,50"), dates, units,
  price math, matching, rules: all deterministic. The LLM appears exactly
  twice - extraction of irregular text (step 2) and line-level risk
  assessment (step 4) - and both calls return Pydantic-validated
  structured output.
- **Lowest autonomy that passes the evals.** A workflow with fixed steps,
  not an agent that decides what to do: more predictability beats more
  flexibility in an operational process.
- **Escalation-only model opinions.** In step 4 a failed rule can never be
  overturned by the model; a hallucinated "all good" cannot approve a bad
  line. This is the main guardrail besides the human confirm.
- **Traceability.** Every run writes `runs/<run_id>/` with one readable
  JSON per step (input summary, full output, duration, LLM usage) plus
  `trace.md` for humans.
- **Degradation is explicit.** Without an API key the pipeline runs fully
  deterministic (code paths + heuristics) and says so; eval cases that
  genuinely need a model are marked `requires_llm` and reported as skipped
  rather than silently failing.

## Quickstart

```bash
pip install -e ".[dev]"

# process one order (steps 1-4; nothing is written without confirmation)
orderflow process data/samples/ordine_email_acciaierie_rossi.eml

# ... and write it to the (mock) ERP after the confirmation prompt
orderflow process data/samples/ordine_email_acciaierie_rossi.eml --write

orderflow erp            # see what landed in the ERP
```

Optional LLM mode (irregular text extraction + line risk assessment):

```bash
cp .env.example .env     # set ANTHROPIC_API_KEY
export ANTHROPIC_API_KEY=sk-ant-...
orderflow process ...    # picks the model up automatically (claude-opus-5)
```

## Evals - one command

```bash
make eval        # or: orderflow eval
```

25 real-shaped cases (clean orders in five formats, Italian/English/German,
decimal-format traps, kg conversions, unknown products/customers, price
deviations at both thresholds, MOQ, credit limit, duplicate references,
past delivery dates, quoted-reply noise, prose orders). Each case pins
`today`, seeds the ERP when needed, and checks **objectively**: line
counts, SKUs, quantities, prices, verdicts, exception codes.

The report (`evals/report.json`) breaks scores down **per component**
(extract / reconcile / check) so error analysis points at the step that
fails most, and ends with a **latency & cost profile per step**.

Where no objective check exists (fidelity of free-text extraction) an LLM
judge applies the rubric in `evals/rubric.md`:

```bash
make eval-judge  # needs an API key; judge scores never gate pass/fail
```

Current state: deterministic mode passes 24/24 applicable cases (1 case
requires the LLM and is skipped without a key).

## The MVP app

```bash
orderflow serve     # http://127.0.0.1:8000
```

Upload button → the document that arrives → the extraction with per-line
verdicts → **"Conferma e scrivi in ERP"** → the row appears in the ERP
panel. No code on screen: it demos the process, not the implementation.

### Static demo (Netlify)

`app/netlify/` is a self-contained static build of the same page for
business-facing demos: the five bundled samples replay real pipeline runs
(frozen by `scripts/build_demo.py`), uploaded `.txt`/`.csv`/`.eml` files
are processed by a small deterministic engine in the browser, and the ERP
is simulated in `localStorage`. Deploy = point Netlify at the repo
(`netlify.toml` already publishes `app/netlify`), or drag the folder into
the Netlify UI. Rebuild with `make demo`.

## Repository layout

```
src/order_workflow/
  models.py            # Pydantic contracts shared by all steps
  pipeline.py          # the 5-step orchestrator + human-confirm gate
  steps/               # normalize, extract, reconcile, check, erp_write
  reference.py         # master data behind SQL queries (SQLite in-memory)
  erp.py               # mock ERP (SQLite)
  llm.py               # Anthropic client wrapper (structured outputs)
  evals.py             # eval harness
  tracing.py           # per-run, per-step readable traces
  web.py + webstatic/  # FastAPI MVP app
data/reference/        # customers, products, list prices (fictional)
data/samples/          # 5 sample orders (eml, csv, xlsx, pdf, txt)
evals/cases/           # 25 cases: input + hand-authored ground truth
scripts/               # generate_data.py, build_demo.py
tests/                 # unit + e2e tests (all deterministic)
```

## Honest limits / next steps

- The ERP and master data are fictional stand-ins; the write step is where
  a real ERP API (or RPA) plugs in, keeping the confirm gate.
- OCR is a fallback hook (`pip install -e ".[ocr]"` + tesseract), not
  exercised by the eval set yet.
- The static demo's in-browser engine covers the deterministic paths only;
  the LLM lives in the Python backend.
- With an API key, the next step per the method is component-level error
  analysis on the LLM cases (extraction fidelity first), then tuning the
  eval set with real failure cases.
