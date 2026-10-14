# Conventions

Watchman reads this file (via `read_conventions()`) when it reviews this repo. Keep it
short and concrete. Every rule here should be something a reviewer can check.

## Naming
- Modules and functions: `snake_case`. Classes: `PascalCase`. Constants: `UPPER_SNAKE`.
- Booleans read as predicates: `is_clean`, `has_findings`. Avoid names like `flag` or `check2`.

## Error handling
- Never use a bare `except:` or `except Exception:` unless it re-raises or logs with
  context *and* there is a comment explaining why it must be broad.
- Never swallow an error silently (`except ...: pass`). Return a typed error or re-raise.
- External process and network failures surface as `ToolError` or a finding. They must
  never crash the review.

## Types
- All public functions have full type annotations; `mypy --strict` passes on `src/`.
- Prefer Pydantic models or dataclasses over loose `dict[str, Any]` at module boundaries.

## Tests
- Every new module ships with tests in `tests/`.
- Tests must be able to fail: assert on concrete values. Don't just check
  `is not None` or that the code ran without raising.
- Don't mock the unit under test. Mock only network, LLM calls, and the clock.
- Tests never hit the network or a real LLM unless marked `@pytest.mark.network`.

## Banned patterns
- `print()` in library code (use the `rich` console in `cli.py` / `report.py` only).
- `shell=True` in `subprocess` calls.
- Committing API keys, tokens, or `.env` files.
- Comments that restate the code.

## Scope
- One concern per commit. Refactors and behavior changes go in separate commits.
