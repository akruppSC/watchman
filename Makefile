UV ?= uv

.PHONY: install lint typecheck test check results

install:
	$(UV) sync --all-extras

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .

typecheck:
	$(UV) run mypy

test:
	$(UV) run pytest -m "not network and not docker"

check: lint typecheck test

results:
	$(UV) run python -m evals.harness report
