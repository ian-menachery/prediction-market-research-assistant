.PHONY: install install-dev install-locked run test lint lock

install:
	pip install -r requirements.txt

install-dev:
	pip install -r requirements-dev.txt

# Install the exact pinned set (reproducible). Generate the locks with `make lock`.
install-locked:
	pip install -r requirements.lock -r requirements-dev.lock

# Regenerate lockfiles from the >= source files (needs pip-tools + network).
lock:
	pip-compile --quiet --output-file=requirements.lock requirements.txt
	pip-compile --quiet --output-file=requirements-dev.lock requirements-dev.txt

test:
	PYTHONPATH=src pytest -q

lint:
	ruff check src tests

# src/ is on PYTHONPATH so `research` resolves as a top-level package.
# (app.py is scaffolded in a later Phase 1 slice.)
run:
	PYTHONPATH=src python -m research.app
