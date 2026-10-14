from __future__ import annotations

import pytest

from watchman_review.config import Config, KeySource, Provider, SandboxKind
from watchman_review.llm.catalog import CatalogError, ModelInfo
from watchman_review.setup_wizard import SetupDeps, run_setup


class ScriptedIO:
    def __init__(self, answers: list[str], confirms: list[bool] | None = None) -> None:
        self.answers = list(answers)
        self.confirms = list(confirms or [])
        self.output: list[str] = []
        self.hidden_prompts: list[str] = []

    def prompt(self, text: str, default: str | None = None, *, hide: bool = False) -> str:
        if hide:
            self.hidden_prompts.append(text)
        ans = self.answers.pop(0)
        return default or "" if ans == "" else ans

    def confirm(self, text: str, default: bool = True) -> bool:
        return self.confirms.pop(0)

    def echo(self, text: str) -> None:
        self.output.append(text)


CATALOG = [ModelInfo(id="vendor/good-model", input_cost_per_mtok=3.0, output_cost_per_mtok=15.0)]


def test_openrouter_with_env_key_and_catalog_pricing() -> None:
    io = ScriptedIO(
        # provider, base url, env var, model, monthly cap, per-review, sandbox
        ["openrouter", "", "", "vendor/good-model", "30", "", "auto"]
    )
    deps = SetupDeps(
        catalog=lambda cfg, key: CATALOG,
        keyring_setter=None,
        environ={"OPENROUTER_API_KEY": "sk-x"},
    )
    cfg = run_setup(io, Config(), deps)
    assert cfg.llm.provider is Provider.OPENROUTER
    assert cfg.llm.model == "vendor/good-model"
    assert cfg.llm.api_key_source is KeySource.ENV
    assert cfg.llm.input_cost_per_mtok == 3.0
    assert cfg.budget.monthly_cap_usd == 30.0
    assert cfg.llm.base_url == ""
    assert "sk-x" not in cfg.model_dump_json()


def test_keyring_path_stores_secret_not_in_config() -> None:
    stored: dict[tuple[str, str], str] = {}
    io = ScriptedIO(
        ["anthropic", "", "", "secret-value", "claude-x", "3", "15", "", "", "docker"],
        confirms=[True],
    )
    deps = SetupDeps(
        catalog=lambda cfg, key: (_ for _ in ()).throw(CatalogError("offline")),
        keyring_setter=lambda s, u, p: stored.__setitem__((s, u), p),
        environ={},
    )
    cfg = run_setup(io, Config(), deps)
    assert stored == {("watchman-review", "anthropic"): "secret-value"}
    assert cfg.llm.api_key_source is KeySource.KEYRING
    assert cfg.review.sandbox is SandboxKind.DOCKER
    assert cfg.llm.output_cost_per_mtok == 15.0
    assert "secret-value" not in cfg.model_dump_json()
    assert io.hidden_prompts == ["API key"]


def test_unknown_model_requires_confirmation() -> None:
    io = ScriptedIO(
        ["openai", "", "", "typo-model", "vendor/good-model", "", "", "auto"],
        confirms=[False],
    )
    deps = SetupDeps(
        catalog=lambda c, k: CATALOG, keyring_setter=None, environ={"OPENAI_API_KEY": "k"}
    )
    cfg = run_setup(io, Config(), deps)
    assert cfg.llm.model == "vendor/good-model"


def test_ollama_needs_no_key() -> None:
    io = ScriptedIO(["4", "", "llama3", "", "", "subprocess"])
    deps = SetupDeps(catalog=lambda c, k: [], keyring_setter=None, environ={})
    cfg = run_setup(io, Config(), deps)
    assert cfg.llm.provider is Provider.OLLAMA
    assert cfg.llm.api_key_source is KeySource.NONE


@pytest.mark.parametrize("bad", ["9", "nope"])
def test_choose_reprompts_on_invalid(bad: str) -> None:
    io = ScriptedIO([bad, "ollama", "", "m", "", "", "auto"])
    deps = SetupDeps(catalog=lambda c, k: [], keyring_setter=None, environ={})
    cfg = run_setup(io, Config(), deps)
    assert cfg.llm.provider is Provider.OLLAMA
