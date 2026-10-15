"""Configuration loading.

Precedence (lowest to highest):
  built-in defaults < user config (~/.config/watchman/config.toml) < repo `.watchman.toml`

Security: the repo config comes from the code under review, so it is **untrusted**. It may
only set `[review]` options and may only *lower* `[budget]` limits. It can never change the
LLM provider, endpoint, model, or key source, because otherwise a malicious repo could point
Watchman at an attacker-controlled endpoint and exfiltrate the API key.
"""

from __future__ import annotations

import os
import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Any

import tomli_w
from pydantic import BaseModel, ConfigDict, Field

from watchman_review.schema import ReviewMode

KEYRING_SERVICE = "watchman-review"
REPO_CONFIG_NAME = ".watchman.toml"


class Provider(StrEnum):
    OPENROUTER = "openrouter"
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    OLLAMA = "ollama"


class KeySource(StrEnum):
    ENV = "env"
    KEYRING = "keyring"
    NONE = "none"


PROVIDER_DEFAULTS: dict[Provider, dict[str, str]] = {
    Provider.OPENROUTER: {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
    },
    Provider.ANTHROPIC: {
        "base_url": "https://api.anthropic.com",
        "api_key_env": "ANTHROPIC_API_KEY",
    },
    Provider.OPENAI: {"base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY"},
    Provider.OLLAMA: {"base_url": "http://localhost:11434/v1", "api_key_env": ""},
}


class ConfigError(Exception):
    pass


class LLMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Provider = Provider.OPENROUTER
    model: str = ""
    base_url: str = ""
    api_key_env: str = ""
    api_key_source: KeySource = KeySource.ENV
    temperature: float = 0.0
    seed: int | None = 7
    # Reasoning models (e.g. GPT-5) spend hidden reasoning tokens from this allowance;
    # too low a value truncates the final answer.
    max_output_tokens: int = 16000
    timeout_s: float = 300.0
    # Throttle requests per minute per model. Providers cap new accounts (OpenRouter: 20/min
    # per model); parallel batches share this limit instead of triggering 429s.
    max_requests_per_minute: int | None = Field(default=None, ge=1)
    input_cost_per_mtok: float | None = Field(
        default=None, description="USD per 1M input tokens; enables cost tracking."
    )
    output_cost_per_mtok: float | None = None

    def resolved_base_url(self) -> str:
        return (self.base_url or PROVIDER_DEFAULTS[self.provider]["base_url"]).rstrip("/")

    def resolved_key_env(self) -> str:
        return self.api_key_env or PROVIDER_DEFAULTS[self.provider]["api_key_env"]

    @property
    def has_pricing(self) -> bool:
        return self.input_cost_per_mtok is not None and self.output_cost_per_mtok is not None


class BudgetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_tool_calls: int = Field(default=25, ge=0)
    max_tokens_per_review: int = Field(default=300_000, ge=1)
    max_cost_per_review_usd: float = Field(default=0.50, ge=0)
    monthly_cap_usd: float = Field(default=50.0, ge=0)


class SandboxKind(StrEnum):
    AUTO = "auto"
    DOCKER = "docker"
    SUBPROCESS = "subprocess"


class ReviewConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checks: list[str] = Field(
        default_factory=lambda: [
            "secrets",
            "imports",
            "hygiene",
            "scope",
            "quality",
            "ruff",
            "mypy",
            "pytest",
        ]
    )
    # Tuned on the dev split (evals/tune.py): 0.6 + restatement dropping raised precision
    # from 56% to 68% for a 4-point recall cost with the Haiku agent.
    min_confidence: float = Field(default=0.6, ge=0, le=1)
    block_min_confidence: float = Field(default=0.7, ge=0, le=1)
    test_command: list[str] | None = None
    test_timeout_s: float = 300.0
    sandbox: SandboxKind = SandboxKind.AUTO
    docker_image: str = "python:3.12-slim"
    conventions_file: str = "CONVENTIONS.md"
    max_diff_files_warn: int = 15
    max_diff_lines_warn: int = 600
    # User-config only: a reviewed repo must not be able to opt you into paid LLM modes.
    default_mode: ReviewMode = ReviewMode.DETERMINISTIC


class OverseerTrigger(StrEnum):
    RISKY = "risky"  # only when the review looks risky or unsettled
    ALWAYS = "always"
    NEVER = "never"


class OverseerConfig(BaseModel):
    """The larger model that double-checks a review, used mainly as an opt-in `--deep` tool.

    User config only: a reviewed repo must not be able to switch on (and bill you for) a
    more expensive model.

    On the v1 dev split, running it automatically did not pay for itself: triggered by
    risk it added no recall for +80% cost, and always-on it added 7 points of recall for
    2.4x the cost while flagging more on clean diffs. So it is off by default and used
    deliberately, where a rare, expensive second opinion is worth it.
    """

    model_config = ConfigDict(extra="forbid")

    auto: bool = Field(
        default=False,
        description="Also run automatically on every review, following `trigger`. Off by "
        "default; `watchman review --deep` runs it for one review regardless.",
    )
    model: str = "openai/gpt-6-astra"
    trigger: OverseerTrigger = OverseerTrigger.RISKY
    input_cost_per_mtok: float | None = None
    output_cost_per_mtok: float | None = None
    # Rarely used, so each run can afford to look hard.
    max_cost_usd: float = Field(default=0.50, ge=0)
    max_tool_calls: int = Field(default=15, ge=0)
    max_spot_checks: int = Field(default=12, ge=1)
    # Risk triggers (any one fires the overseer when trigger = "risky").
    large_diff_lines: int = Field(default=300, ge=1)
    large_diff_files: int = Field(default=8, ge=1)
    # Feedback: send the overseer's notes back to the primary reviewer for one more pass
    # when it rejected or added at least this many findings, or rejected this share of them.
    feedback_min_issues: int = Field(default=3, ge=1)
    feedback_reject_fraction: float = Field(default=0.5, ge=0, le=1)
    feedback_max_rounds: int = Field(default=1, ge=0, le=2)
    second_pass_tool_calls: int = Field(default=12, ge=0)


class LivePolicy(StrEnum):
    ADVISORY = "advisory"  # feedback only; the worker is never stopped
    BLOCKING = "blocking"  # a BLOCK verdict stops a commit / "done" until fixed or the cap hits


class LiveConfig(BaseModel):
    """Live mode: Watchman supervising a coding agent at checkpoints. User config only: a
    reviewed repo must not control when (and how expensively) its own reviewer runs."""

    model_config = ConfigDict(extra="forbid")

    policy: LivePolicy = LivePolicy.ADVISORY
    round_cap: int = Field(default=3, ge=1, le=10)  # review/fix rounds per checkpoint kind
    # Layer 1 checks that are cheap enough to run on every test run; the worker just ran the tests.
    test_run_checks: list[str] = Field(
        default_factory=lambda: [
            *("secrets", "imports", "hygiene", "scope", "quality"),
            *("ruff", "mypy"),
        ]
    )
    test_run_mode: ReviewMode = ReviewMode.DETERMINISTIC
    commit_mode: ReviewMode = ReviewMode.AGENT
    done_mode: ReviewMode = ReviewMode.AGENT
    max_findings_shown: int = Field(default=6, ge=1, le=25)


USER_ONLY_REVIEW_KEYS = {"default_mode"}


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llm: LLMConfig = Field(default_factory=LLMConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    overseer: OverseerConfig = Field(default_factory=OverseerConfig)
    live: LiveConfig = Field(default_factory=LiveConfig)


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "watchman"


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "watchman"


def user_config_path() -> Path:
    return Path(os.environ.get("WATCHMAN_CONFIG") or config_dir() / "config.toml")


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML in {path}: {e}") from e


def _merge_repo_config(cfg: Config, repo_data: dict[str, Any], source: Path) -> list[str]:
    """Apply the untrusted repo config. Returns warnings for ignored keys."""
    warnings: list[str] = []
    for section in repo_data:
        if section not in ("review", "budget"):
            warnings.append(f"{source}: ignored [{section}] (not allowed in repo config)")

    review = repo_data.get("review")
    if isinstance(review, dict):
        for key in USER_ONLY_REVIEW_KEYS & review.keys():
            warnings.append(f"{source}: review.{key} may only be set in user config; ignored")
        review = {k: v for k, v in review.items() if k not in USER_ONLY_REVIEW_KEYS}
        merged = cfg.review.model_dump() | review
        cfg.review = ReviewConfig.model_validate(merged)

    budget = repo_data.get("budget")
    if isinstance(budget, dict):
        current = cfg.budget.model_dump()
        for key, value in budget.items():
            if key not in current:
                warnings.append(f"{source}: unknown budget key {key!r}")
            elif isinstance(value, int | float) and value <= current[key]:
                current[key] = value
            else:
                warnings.append(f"{source}: budget.{key} may only be lowered; ignored")
        cfg.budget = BudgetConfig.model_validate(current)
    return warnings


def load_config(
    repo_root: Path | None = None, *, repo_config_text: str | None = None
) -> tuple[Config, list[str]]:
    """Load the effective config. Returns (config, warnings).

    Pass `repo_config_text` to use a specific version of `.watchman.toml` (the review
    reads it from the *base* revision so a change cannot relax its own review settings).
    """
    try:
        cfg = Config.model_validate(_read_toml(user_config_path()))
    except ValueError as e:
        raise ConfigError(f"invalid user config {user_config_path()}: {e}") from e

    warnings: list[str] = []
    if repo_root is not None or repo_config_text is not None:
        repo_path = (repo_root or Path(".")) / REPO_CONFIG_NAME
        if repo_config_text is not None:
            try:
                repo_data = tomllib.loads(repo_config_text)
            except tomllib.TOMLDecodeError as e:
                raise ConfigError(f"invalid TOML in {repo_path}: {e}") from e
        else:
            repo_data = _read_toml(repo_path)
        if repo_data:
            try:
                warnings += _merge_repo_config(cfg, repo_data, repo_path)
            except ValueError as e:
                raise ConfigError(f"invalid repo config {repo_path}: {e}") from e
    return cfg, warnings


def save_user_config(cfg: Config, path: Path | None = None) -> Path:
    path = path or user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Only values that differ from the built-in defaults are written. Saving every field
    # pinned old defaults into the file, so later improvements to a default (a tuned
    # min_confidence, say) silently never reached existing users.
    data = cfg.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(tomli_w.dumps(data))
    tmp.chmod(0o600)
    tmp.replace(path)
    return path


def keyring_backend_usable() -> bool:
    """True only if `keyring` is installed *and* a real backend (Secret Service, macOS
    Keychain, Windows Credential Locker, ...) is available. On headless Linux without a
    keyring daemon, keyring falls back to a 'fail' backend that raises on every call."""
    try:
        import keyring
        from keyring.backends import fail
    except ImportError:
        return False
    backend = keyring.get_keyring()
    return not isinstance(backend, fail.Keyring) and float(getattr(backend, "priority", 0)) > 0


def resolve_api_key(llm: LLMConfig) -> str | None:
    """Return the API key, or None for providers that need none. Raise if it's missing."""
    if llm.provider is Provider.OLLAMA or llm.api_key_source is KeySource.NONE:
        return None
    if llm.api_key_source is KeySource.KEYRING:
        try:
            import keyring
            from keyring.errors import KeyringError
        except ImportError as e:
            raise ConfigError(
                "api_key_source = 'keyring' but the keyring package is not installed "
                "(install with the 'keyring' extra)"
            ) from e
        try:
            key = keyring.get_password(KEYRING_SERVICE, llm.provider.value)
        except KeyringError as e:
            raise ConfigError(
                f"could not read the API key from the OS keyring ({type(e).__name__}); "
                f"set {llm.resolved_key_env()} instead and re-run `watchman setup`"
            ) from e
        if not key:
            raise ConfigError(f"no key stored in keyring for {llm.provider}; run `watchman setup`")
        return str(key)
    env = llm.resolved_key_env()
    key = os.environ.get(env, "")
    if not key:
        raise ConfigError(f"environment variable {env} is not set; run `watchman setup`")
    return key
