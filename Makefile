PY ?= python3
VENV ?= .venv
BIN = $(VENV)/bin
export PYTHONPATH := src:.

.PHONY: setup sample test examples example-orders example-payments example-langgraph clean
setup:
	$(PY) -m venv $(VENV) && $(BIN)/pip install -q -r requirements.txt
sample:
	$(BIN)/python scripts/generate_sample.py
test:
	$(BIN)/python -m pytest -q
examples: example-orders example-payments example-langgraph
example-orders:
	$(BIN)/python -m examples.order_exception.run
example-payments:
	$(BIN)/python -m examples.vendor_payment.run
example-langgraph:
	$(BIN)/python -m examples.langgraph_integration.run
	$(BIN)/python -m examples.langgraph_integration.run --no-langgraph
clean:
	rm -rf out .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
