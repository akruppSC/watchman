from __future__ import annotations

import stat
from pathlib import Path

import pytest

from watchman_review.config import (
    Config,
    ConfigError,
    KeySource,
    LLMConfig,
    Provider,
    load_config,
    resolve_api_key,
    save_user_config,
    user_config_path,
)


def test_defaults_when_no_files(tmp_path: Path) -> None:
    cfg, warnings = load_config(tmp_path)
    assert cfg.budget.max_tool_calls == 25
    assert cfg.budget.monthly_cap_usd == 50.0
    assert warnings == []


def test_save_and_load_roundtrip_with_private_perms() -> None:
    cfg = Config()
    cfg.llm.provider = Provider.ANTHROPIC
    cfg.llm.model = "some-model"
    path = save_user_config(cfg)
    assert path == user_config_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    loaded, _ = load_config(None)
    assert loaded.llm.provider is Provider.ANTHROPIC
    assert loaded.llm.model == "some-model"


def test_repo_config_cannot_override_llm(tmp_path: Path) -> None:
    (tmp_path / ".watchman.toml").write_text(
        '[llm]\nbase_url = "https://evil.example"\n[review]\nmin_confidence = 0.8\n'
    )
    cfg, warnings = load_config(tmp_path)
    assert cfg.llm.base_url == ""
    assert cfg.review.min_confidence == 0.8
    assert any("[llm]" in w for w in warnings)


def test_repo_config_cannot_select_paid_mode(tmp_path: Path) -> None:
    (tmp_path / ".watchman.toml").write_text('[review]\ndefault_mode = "agent"\n')
    cfg, warnings = load_config(tmp_path)
    assert cfg.review.default_mode == "deterministic"
    assert any("default_mode" in w for w in warnings)


def test_repo_config_can_only_lower_budget(tmp_path: Path) -> None:
    (tmp_path / ".watchman.toml").write_text(
        "[budget]\nmax_tool_calls = 10\nmonthly_cap_usd = 9999\n"
    )
    cfg, warnings = load_config(tmp_path)
    assert cfg.budget.max_tool_calls == 10
    assert cfg.budget.monthly_cap_usd == 50.0
    assert any("only be lowered" in w for w in warnings)


def test_invalid_toml_raises(tmp_path: Path) -> None:
    (tmp_path / ".watchman.toml").write_text("[review\n")
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_resolve_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    assert resolve_api_key(LLMConfig(provider=Provider.OPENROUTER)) == "sk-test"


def test_resolve_key_custom_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_KEY", "abc")
    assert resolve_api_key(LLMConfig(provider=Provider.OPENAI, api_key_env="MY_KEY")) == "abc"


def test_missing_key_raises() -> None:
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        resolve_api_key(LLMConfig(provider=Provider.OPENROUTER))


def test_keyring_read_failure_is_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import keyring
    from keyring.backends import fail

    monkeypatch.setattr(keyring, "get_keyring", lambda: fail.Keyring())
    llm = LLMConfig(provider=Provider.OPENROUTER, api_key_source=KeySource.KEYRING)
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        resolve_api_key(llm)


def test_ollama_needs_no_key() -> None:
    assert resolve_api_key(LLMConfig(provider=Provider.OLLAMA)) is None


def test_none_key_source() -> None:
    llm = LLMConfig(provider=Provider.OPENAI, api_key_source=KeySource.NONE)
    assert resolve_api_key(llm) is None


def test_save_writes_only_non_default_values() -> None:
    cfg = Config()
    cfg.llm.model = "vendor/some-model"
    cfg.llm.input_cost_per_mtok = 0.75
    path = save_user_config(cfg)
    text = path.read_text()
    assert 'model = "vendor/some-model"' in text
    assert "min_confidence" not in text and "[budget]" not in text and "[review]" not in text
    loaded, _ = load_config(None)
    assert loaded.llm.model == "vendor/some-model"
    assert loaded.review.min_confidence == Config().review.min_confidence


def test_default_changes_reach_users_who_saved_before() -> None:
    cfg = Config()
    cfg.llm.model = "m"
    path = save_user_config(cfg)
    assert "min_confidence" not in path.read_text()  # not pinned, so it follows the default


def test_saving_all_defaults_roundtrips_empty() -> None:
    path = save_user_config(Config())
    assert path.read_text().strip() == ""
    assert load_config(None)[0] == Config()
