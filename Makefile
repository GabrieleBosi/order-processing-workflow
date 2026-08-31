.PHONY: install test eval eval-judge serve demo erp lint data

install:
	pip install -e ".[dev]"

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
	ruff check src tests scripts
