.DEFAULT_GOAL := check

image_tag ?= logline-server:dev

check: lint test

test:
	uv run pytest -v --tb=native $(pytest_args) tests

lint:
	uvx flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics
	uvx flake8 . --count --exit-zero --max-complexity=10 --max-line-length=127 --statistics

image:
	podman build -t $(image_tag) .
	# smoke check: the entrypoint starts and responds
	podman run --rm $(image_tag) --help >/dev/null

.PHONY: check test lint image
