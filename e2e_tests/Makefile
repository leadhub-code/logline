.DEFAULT_GOAL := check

check: lint test

test:
	uv run pytest -v --tb=native -p no:logging $(pytest_args) tests

lint:
	uvx ruff check .

.PHONY: check test lint
