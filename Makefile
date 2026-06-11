.PHONY: install run

install:
	pip install -r requirements.txt

# src/ is on PYTHONPATH so `research` resolves as a top-level package.
# (app.py is scaffolded in a later Phase 1 slice.)
run:
	PYTHONPATH=src python -m research.app
