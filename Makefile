.PHONY: install install-dev run test lint

install:
	pip install -r requirements.txt

install-dev:
	pip install -r requirements-dev.txt

test:
	PYTHONPATH=src pytest -q

lint:
	ruff check src tests

# src/ is on PYTHONPATH so `research` resolves as a top-level package.
# (app.py is scaffolded in a later Phase 1 slice.)
run:
	PYTHONPATH=src python -m research.app
