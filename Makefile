.DEFAULT_GOAL := check

# Run the full quality gate (lint + tests) for every component.
check:
	$(MAKE) -C agent check
	$(MAKE) -C server check
	$(MAKE) -C e2e_tests check

# Run only the test suites.
test:
	$(MAKE) -C agent test
	$(MAKE) -C server test
	$(MAKE) -C e2e_tests test

# Run only the linters.
lint:
	$(MAKE) -C agent lint
	$(MAKE) -C server lint
	$(MAKE) -C e2e_tests lint

# Build the server container image.
image:
	$(MAKE) -C server image

# Backwards-compatible alias for the previous aggregate target.
run_e2e_tests: test

.PHONY: check test lint image run_e2e_tests
