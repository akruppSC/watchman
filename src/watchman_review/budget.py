"""Per-review budgets and the persistent monthly spend ledger."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from watchman_review.config import BudgetConfig, LLMConfig, data_dir
from watchman_review.llm.types import CompletionRequest, LLMClient, LLMResponse
from watchman_review.schema import Usage


class BudgetExceeded(Exception):
    """A per-review or monthly limit would be exceeded."""


class LedgerEntry(BaseModel):
    ts: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    purpose: str


class Ledger:
    """Append-only JSONL record of real (non-cached) LLM spend. Safe across threads."""

    _lock = threading.Lock()

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or data_dir() / "ledger.jsonl"

    def record(self, entry: LedgerEntry) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a") as fh:
            fh.write(entry.model_dump_json() + "\n")

    def month_total(self, now: datetime | None = None) -> float:
        now = now or datetime.now(UTC)
        prefix = now.strftime("%Y-%m")
        total = 0.0
        try:
            lines = self.path.read_text().splitlines()
        except FileNotFoundError:
            return 0.0
        for line in lines:
            if not line.strip():
                continue
            try:
                entry = LedgerEntry.model_validate(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue  # a corrupt line must not disable the cap for valid ones
            if entry.ts.startswith(prefix):
                total += entry.cost_usd
        return total


def estimate_cost(llm: LLMConfig, resp: LLMResponse) -> float:
    if resp.cost_usd is not None:
        return resp.cost_usd
    if llm.has_pricing:
        assert llm.input_cost_per_mtok is not None and llm.output_cost_per_mtok is not None
        return (
            resp.input_tokens * llm.input_cost_per_mtok
            + resp.output_tokens * llm.output_cost_per_mtok
        ) / 1_000_000
    return 0.0


class ReviewBudget:
    """Tracks spend for a single review and enforces its limits."""

    def __init__(self, cfg: BudgetConfig) -> None:
        self.cfg = cfg
        self.usage = Usage()

    @property
    def total_tokens(self) -> int:
        return self.usage.input_tokens + self.usage.output_tokens

    def check_llm_call(self) -> None:
        if self.total_tokens >= self.cfg.max_tokens_per_review:
            raise BudgetExceeded(
                f"token budget exhausted ({self.total_tokens}/{self.cfg.max_tokens_per_review})"
            )
        if self.cfg.max_cost_per_review_usd and (
            self.usage.cost_usd >= self.cfg.max_cost_per_review_usd
        ):
            raise BudgetExceeded(
                f"cost budget exhausted (${self.usage.cost_usd:.4f}/"
                f"${self.cfg.max_cost_per_review_usd:.2f})"
            )

    def tool_calls_remaining(self) -> int:
        return max(0, self.cfg.max_tool_calls - self.usage.tool_calls)

    def record_tool_call(self) -> None:
        if self.tool_calls_remaining() <= 0:
            raise BudgetExceeded(f"tool-call budget exhausted ({self.cfg.max_tool_calls})")
        self.usage.tool_calls += 1


class MeteredClient:
    """Wraps an LLM client: enforces per-review and monthly limits, records usage."""

    def __init__(
        self,
        inner: LLMClient,
        *,
        llm_cfg: LLMConfig,
        budget: ReviewBudget,
        ledger: Ledger | None,
        purpose: str = "review",
    ) -> None:
        self._inner = inner
        self._llm_cfg = llm_cfg
        self._budget = budget
        self._ledger = ledger
        self._purpose = purpose

    @property
    def model(self) -> str:
        return self._inner.model

    def complete(self, request: CompletionRequest) -> LLMResponse:
        self._budget.check_llm_call()
        if self._ledger is not None:
            cap = self._budget.cfg.monthly_cap_usd
            spent = self._ledger.month_total()
            if cap and spent >= cap:
                raise BudgetExceeded(f"monthly cap reached (${spent:.2f}/${cap:.2f})")
        resp = self._inner.complete(request)
        cost = estimate_cost(self._llm_cfg, resp)
        u = self._budget.usage
        u.llm_calls += 1
        u.input_tokens += resp.input_tokens
        u.output_tokens += resp.output_tokens
        u.cached_input_tokens += resp.cached_input_tokens
        # Cached responses count toward the per-review budget (so evals behave the same
        # on replay) but not toward the real-money ledger.
        u.cost_usd += cost
        u.model = u.model or resp.model or self._inner.model
        if self._ledger is not None and not resp.cached:
            self._ledger.record(
                LedgerEntry(
                    ts=datetime.now(UTC).isoformat(),
                    model=resp.model or self._inner.model,
                    input_tokens=resp.input_tokens,
                    output_tokens=resp.output_tokens,
                    cost_usd=cost,
                    purpose=self._purpose,
                )
            )
        return resp
