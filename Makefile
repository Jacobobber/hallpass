# Convenience targets. uv is the only prerequisite; every target is just a
# uv command you can also run directly (see the commands below), so nothing
# here is required, including make itself.
.PHONY: help install test lint typecheck fmt catalog demo check

help:
	@echo "make test       - run the suite"
	@echo "make lint       - ruff check"
	@echo "make typecheck  - mypy --strict on src"
	@echo "make fmt        - ruff format"
	@echo "make catalog    - regenerate docs/CATALOG.md"
	@echo "make demo       - run the quickstart example"
	@echo "make check      - test + lint + typecheck + catalog --check"

install:
	uv sync --group dev

test:
	uv run --group dev pytest -q

lint:
	uv run --with ruff ruff check .

typecheck:
	uv run --group dev --with mypy mypy --strict src

fmt:
	uv run --with ruff ruff format .

catalog:
	python scripts/gen_catalog.py

demo:
	uv run python examples/quickstart.py

check: test lint typecheck
	python scripts/gen_catalog.py --check
