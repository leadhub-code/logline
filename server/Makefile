.DEFAULT_GOAL := check

image_tag ?= logline-server:dev

check: lint test

test:
	uv run pytest -v --tb=native $(pytest_args) tests

lint:
	uvx ruff check .

image:
	podman build -t $(image_tag) .
	# smoke check: the entrypoint starts and responds
	podman run --rm $(image_tag) --help >/dev/null

.PHONY: check test lint image
