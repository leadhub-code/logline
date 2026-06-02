.DEFAULT_GOAL := check

check: test

test:
	uv run pytest -v --tb=native -p no:logging $(pytest_args) tests

.PHONY: check test
