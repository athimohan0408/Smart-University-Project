# Thin aliases for people who type `make`. The justfile is the primary task runner (see CLAUDE.md).
PYTHON ?= python

.PHONY: eval eval-baseline calibrate test serve

eval:
	$(PYTHON) -m rag_core.eval.run_eval

eval-baseline:
	$(PYTHON) -m rag_core.eval.run_eval --write-baseline

calibrate:
	$(PYTHON) -m rag_core.scoring.calibrate

test:
	$(PYTHON) -m pytest

serve:
	$(PYTHON) -m rag_core.api
