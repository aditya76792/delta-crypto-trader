# Delta Crypto Trader — developer Makefile
PYTHON ?= python3
.PHONY: install install-dev test lint instruments check history backtest validate replay paper live kill status

install:      ; $(PYTHON) -m pip install -e .
install-dev:  ; $(PYTHON) -m pip install -e ".[dev]"
test:         ; $(PYTHON) -m pytest
lint:         ; $(PYTHON) -m ruff check .
instruments:  ; $(PYTHON) -m main instruments
check:        ; $(PYTHON) -m main check
history:      ; $(PYTHON) -m main fetch-history --days 90
backtest:     ; $(PYTHON) -m main backtest --all --days 60
validate:     ; $(PYTHON) -m main backtest --validate --days 90
replay:       ; $(PYTHON) -m main replay --days 7
paper:        ; $(PYTHON) -m main paper
live:         ; $(PYTHON) -m main live --confirm-live
kill:         ; $(PYTHON) -m main kill
status:       ; $(PYTHON) -m main status
