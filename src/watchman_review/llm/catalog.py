"""Model discovery and pricing lookup, used by `watchman setup`."""

from __future__ import annotations

import httpx
from pydantic import BaseModel

from watchman_review.config import LLMConfig, Provider


class ModelInfo(BaseModel):
    id: str
    input_cost_per_mtok: float | None = None
    output_cost_per_mtok: float | None = None


class CatalogError(Exception):
    pass


def list_models(
    cfg: LLMConfig, api_key: str | None, http: httpx.Client | None = None
) -> list[ModelInfo]:
    """List models the provider offers. OpenRouter includes pricing."""
    http = http or httpx.Client(timeout=20.0)
    base = cfg.resolved_base_url()
    headers: dict[str, str] = {}
    if cfg.provider is Provider.ANTHROPIC:
        url = f"{base}/v1/models?limit=1000"
        if api_key:
            headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    else:
        url = f"{base}/models"
        if api_key:
            headers = {"Authorization": f"Bearer {api_key}"}
    try:
        resp = http.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        raise CatalogError(f"could not list models from {url}: {e}") from e

    out: list[ModelInfo] = []
    for item in data.get("data") or data.get("models") or []:
        mid = item.get("id") or item.get("name")
        if not mid:
            continue
        info = ModelInfo(id=str(mid))
        pricing = item.get("pricing")
        if isinstance(pricing, dict):
            try:
                # Round away float noise (0.0000001 * 1e6 == 0.09999999999999999).
                info.input_cost_per_mtok = round(float(pricing["prompt"]) * 1_000_000, 6)
                info.output_cost_per_mtok = round(float(pricing["completion"]) * 1_000_000, 6)
            except (KeyError, TypeError, ValueError):
                pass
        out.append(info)
    return out
