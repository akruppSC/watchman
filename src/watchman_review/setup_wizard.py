"""Interactive first-run setup: provider, model, key source, budgets.

API key *values* are never written to the config file. The config records the env var
name to read, or that the key lives in the OS keyring.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from watchman_review.config import (
    KEYRING_SERVICE,
    PROVIDER_DEFAULTS,
    Config,
    KeySource,
    Provider,
    SandboxKind,
    keyring_backend_usable,
)
from watchman_review.llm.catalog import CatalogError, ModelInfo, list_models


class SetupIO(Protocol):
    def prompt(self, text: str, default: str | None = None, *, hide: bool = False) -> str: ...
    def confirm(self, text: str, default: bool = True) -> bool: ...
    def echo(self, text: str) -> None: ...


KeyringSetter = Callable[[str, str, str], None]
Catalog = Callable[[Config, str | None], list[ModelInfo]]


def _default_catalog(cfg: Config, key: str | None) -> list[ModelInfo]:
    return list_models(cfg.llm, key)


class KeyringStoreError(Exception):
    """Storing the key in the OS keyring failed."""


def _default_keyring_setter() -> KeyringSetter | None:
    if not keyring_backend_usable():
        return None
    import keyring
    from keyring.errors import KeyringError

    def setter(service: str, user: str, secret: str) -> None:
        try:
            keyring.set_password(service, user, secret)
        except KeyringError as e:
            raise KeyringStoreError(f"{type(e).__name__}: {e}") from e

    return setter


@dataclass
class SetupDeps:
    catalog: Catalog = _default_catalog
    keyring_setter: KeyringSetter | None = field(default_factory=_default_keyring_setter)
    environ: dict[str, str] = field(default_factory=lambda: dict(os.environ))


def _choose(io: SetupIO, text: str, options: list[str], default: str) -> str:
    io.echo(text)
    for i, opt in enumerate(options, 1):
        io.echo(f"  {i}) {opt}")
    while True:
        raw = io.prompt("Choice", default).strip()
        if raw in options:
            return raw
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        io.echo(f"Please enter 1-{len(options)} or one of: {', '.join(options)}")


def _parse_optional_float(raw: str) -> float | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def run_setup(io: SetupIO, existing: Config, deps: SetupDeps | None = None) -> Config:
    deps = deps or SetupDeps()
    cfg = existing.model_copy(deep=True)

    io.echo("Watchman setup. API keys are never written to the config file.\n")

    provider = Provider(
        _choose(
            io,
            "LLM provider for the reviewer:",
            [p.value for p in Provider],
            cfg.llm.provider.value,
        )
    )
    if provider is not cfg.llm.provider:
        cfg.llm.base_url = ""
        cfg.llm.api_key_env = ""
        cfg.llm.input_cost_per_mtok = cfg.llm.output_cost_per_mtok = None
    cfg.llm.provider = provider

    default_base = PROVIDER_DEFAULTS[provider]["base_url"]
    base = io.prompt("API base URL", cfg.llm.base_url or default_base).strip()
    cfg.llm.base_url = "" if base == default_base else base

    key: str | None = None
    if provider is Provider.OLLAMA:
        cfg.llm.api_key_source = KeySource.NONE
    else:
        env_name = io.prompt("Env var holding the API key", cfg.llm.resolved_key_env()).strip()
        cfg.llm.api_key_env = (
            "" if env_name == PROVIDER_DEFAULTS[provider]["api_key_env"] else env_name
        )
        key = deps.environ.get(env_name) or None
        cfg.llm.api_key_source = KeySource.ENV
        if key:
            io.echo(f"Found {env_name} in the environment.")
        elif deps.keyring_setter is None:
            io.echo(
                f"{env_name} is not set, and no OS keyring backend is available on this "
                "machine, so the key can't be stored securely here."
            )
        elif io.confirm(
            f"{env_name} is not set. Store a key in the OS keyring instead?", default=True
        ):
            secret = io.prompt("API key", hide=True).strip()
            if secret:
                try:
                    deps.keyring_setter(KEYRING_SERVICE, provider.value, secret)
                except KeyringStoreError as e:
                    io.echo(f"Could not store the key in the OS keyring ({e}).")
                else:
                    cfg.llm.api_key_source = KeySource.KEYRING
                    key = secret
                    io.echo("Key stored in keyring.")
        if not key:
            io.echo(
                f"Set it in your shell before running reviews: `export {env_name}=...` "
                "(add it to ~/.bashrc to persist)."
            )

    models: list[ModelInfo] = []
    try:
        models = deps.catalog(cfg, key)
        io.echo(f"Provider lists {len(models)} models.")
    except CatalogError as e:
        io.echo(f"Could not fetch model list ({e}); continuing without validation.")

    while True:
        model = io.prompt("Reviewer model id", cfg.llm.model or None).strip()
        if not model:
            io.echo("A model id is required.")
            continue
        if models and not any(m.id == model for m in models):
            close = [m.id for m in models if model.lower() in m.id.lower()][:8]
            hint = f" Similar: {', '.join(close)}" if close else ""
            if not io.confirm(f"{model!r} is not in the provider's list.{hint} Use anyway?", False):
                continue
        break
    cfg.llm.model = model

    info = next((m for m in models if m.id == model), None)
    if info and info.input_cost_per_mtok is not None:
        cfg.llm.input_cost_per_mtok = info.input_cost_per_mtok
        cfg.llm.output_cost_per_mtok = info.output_cost_per_mtok
        io.echo(
            f"Pricing: ${info.input_cost_per_mtok:.2f}/M in, "
            f"${info.output_cost_per_mtok:.2f}/M out."
        )
    elif provider is not Provider.OLLAMA:
        io.echo("Pricing unknown. Enter it to enable cost caps (blank to skip).")
        cfg.llm.input_cost_per_mtok = _parse_optional_float(
            io.prompt("USD per 1M input tokens", _fmt(cfg.llm.input_cost_per_mtok))
        )
        cfg.llm.output_cost_per_mtok = _parse_optional_float(
            io.prompt("USD per 1M output tokens", _fmt(cfg.llm.output_cost_per_mtok))
        )
        if not cfg.llm.has_pricing:
            io.echo(
                "Warning: without pricing, dollar budgets cannot be enforced "
                "(token caps still apply)."
            )

    cap = _parse_optional_float(
        io.prompt("Monthly spend cap (USD)", _fmt(cfg.budget.monthly_cap_usd))
    )
    if cap is not None:
        cfg.budget.monthly_cap_usd = cap
    per = _parse_optional_float(
        io.prompt("Max cost per review (USD)", _fmt(cfg.budget.max_cost_per_review_usd))
    )
    if per is not None:
        cfg.budget.max_cost_per_review_usd = per

    cfg.review.sandbox = SandboxKind(
        _choose(
            io,
            "Sandbox for running tests/linters:",
            [s.value for s in SandboxKind],
            cfg.review.sandbox.value,
        )
    )
    _setup_overseer(io, cfg, models)
    return cfg


def _setup_overseer(io: SetupIO, cfg: Config, models: list[ModelInfo]) -> None:
    io.echo(
        "\nThe overseer is a larger, pricier model that double-checks a review "
        "(spot-checks findings, digs into hard questions, briefs the primary reviewer). "
        "`watchman review --deep` runs it on demand."
    )
    cfg.overseer.model = io.prompt("Overseer model id", cfg.overseer.model).strip()
    info = next((m for m in models if m.id == cfg.overseer.model), None)
    if info and info.input_cost_per_mtok is not None:
        cfg.overseer.input_cost_per_mtok = info.input_cost_per_mtok
        cfg.overseer.output_cost_per_mtok = info.output_cost_per_mtok
        out_cost = info.output_cost_per_mtok or 0.0
        io.echo(f"Pricing: ${info.input_cost_per_mtok:.2f}/M in, ${out_cost:.2f}/M out.")
    cap = _parse_optional_float(
        io.prompt("Max overseer cost per review (USD)", _fmt(cfg.overseer.max_cost_usd))
    )
    if cap is not None:
        cfg.overseer.max_cost_usd = cap
    cfg.overseer.auto = io.confirm(
        "Also run it automatically on reviews that look risky? (costs more; dev data "
        "showed little benefit)",
        default=cfg.overseer.auto,
    )


def _fmt(v: float | None) -> str | None:
    return None if v is None else f"{v:g}"
