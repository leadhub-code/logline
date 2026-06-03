.DEFAULT_GOAL := check

check: lint test

test:
	uv run pytest -v --tb=native $(pytest_args) tests

lint:
	uvx flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics
	uvx flake8 . --count --exit-zero --max-complexity=10 --max-line-length=127 --statistics

.PHONY: check test lint
