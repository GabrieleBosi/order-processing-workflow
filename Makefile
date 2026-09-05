.PHONY: install test eval eval-judge serve demo erp lint data \
        eval-mlflow eval-estimate gate gate-deterministic labels label-sheet calibrate \
        analysis analysis-baseline mlflow-ui

install:
	pip install -e ".[dev,mlflow]"

test:
	python -m pytest -q

# The whole eval set with one command (objective checks; add the LLM judge
# with `make eval-judge` when an API key is configured).
eval:
	python -m order_workflow.cli eval

eval-judge:
	python -m order_workflow.cli eval --judge

serve:
	python -m order_workflow.cli serve

# Rebuild the static Netlify demo from the bundled samples.
demo:
	python scripts/build_demo.py

# Regenerate eval cases and samples (only when changing the case set).
data:
	python scripts/generate_data.py

erp:
	python -m order_workflow.cli erp

lint:
	ruff check src tests scripts evals

# ---------------------------------------------------------------- measurement
# The MLflow loop: estimate the bill, run the suite into one logged run, gate on
# the thresholds, analyse what failed. Store is sqlite:///mlflow.db, gitignored.

# What one full run would cost. Makes no API calls.
eval-estimate:
	python -m evals.run_mlflow --dry-run --judge

# The full suite, logged: params, metrics, per-case tables, one trace per case.
eval-mlflow:
	python -m evals.run_mlflow --judge --budget-usd 1.00

# Fail the build below any per-category acceptance threshold.
# Two configurations, two baselines, two threshold files.
gate:
	python -m evals.gate

gate-deterministic:
	python -m evals.gate --thresholds evals/thresholds_deterministic.yaml

# Write the 20-row hand-label template (never clobbers labels already filled in).
labels:
	python -m evals.make_labels_template

# The sheet the labels are filled in FROM: per judge case, the source document,
# this run's extraction and the judge's score, straight out of the run's trace.
label-sheet:
	python -m evals.label_sheet

# Cohen's kappa, judge vs. hand labels, logged onto the run.
calibrate:
	python -m evals.calibrate_judge

# reports/error_analysis.md from the last logged run.
analysis:
	python -m evals.error_analysis

# Regenerate the committed report from the committed baseline, byte for byte.
analysis-baseline:
	python -m evals.error_analysis --report reports/baseline_llm_report.json

mlflow-ui:
	mlflow ui --backend-store-uri sqlite:///mlflow.db
