.DEFAULT_GOAL := check

image_tag ?= logline-server:dev
git_commit ?= $(shell git rev-parse HEAD 2>/dev/null)$(shell git status --porcelain --untracked-files=no 2>/dev/null | grep -q . && echo -dirty)

check: lint test

test:
	uv run pytest -v --tb=native $(pytest_args) tests

lint:
	uvx ruff check .

image:
	podman build -t $(image_tag) --build-arg GIT_COMMIT=$(git_commit) .
	# smoke check: the entrypoint starts and responds
	podman run --rm $(image_tag) --help >/dev/null
	podman run --rm $(image_tag) --version

.PHONY: check test lint image
