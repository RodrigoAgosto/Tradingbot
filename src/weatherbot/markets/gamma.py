"""Polymarket Gamma API: discover candidate weather markets.

Read-only, unauthenticated. Every fetch records fetched_at so downstream
staleness checks can fail closed.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

import httpx
from pydantic import BaseModel, Field, field_validator

log = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"

_WEATHER_HINT_RE = re.compile(
    r"temperature|°f|°c|degrees|rainfall|precipitation|snowfall|snow\b|highest temp|lowest temp",
    re.IGNORECASE,
)


class GammaMarket(BaseModel):
    id: str
    question: str = ""
    description: str = ""
    end_date: datetime | None = Field(default=None, alias="endDate")
    volume_24h: float = Field(default=0.0, alias="volume24hr")
    outcomes: list[str] = Field(default_factory=list)
    clob_token_ids: list[str] = Field(default_factory=list, alias="clobTokenIds")
    active: bool = True
    closed: bool = False
    fetched_at: datetime | None = None

    model_config = {"populate_by_name": True}

    @field_validator("outcomes", "clob_token_ids", mode="before")
    @classmethod
    def _json_encoded_list(cls, v):
        # Gamma returns these as JSON-encoded strings.
        if isinstance(v, str):
            try:
                return json.loads(v)
            except (ValueError, TypeError):
                return []
        return v or []

    @field_validator("volume_24h", mode="before")
    @classmethod
    def _coerce_volume(cls, v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    def yes_token_id(self) -> str | None:
        """Token id for the 'Yes' outcome (Gamma orders tokens like outcomes)."""
        for outcome, token in zip(self.outcomes, self.clob_token_ids):
            if outcome.strip().lower() == "yes":
                return token
        return self.clob_token_ids[0] if self.clob_token_ids else None

    def no_token_id(self) -> str | None:
        for outcome, token in zip(self.outcomes, self.clob_token_ids):
            if outcome.strip().lower() == "no":
                return token
        return self.clob_token_ids[1] if len(self.clob_token_ids) > 1 else None


def looks_like_weather(market: GammaMarket) -> bool:
    return bool(_WEATHER_HINT_RE.search(f"{market.question} {market.description}"))


def fetch_active_weather_markets(
    client: httpx.Client, page_size: int = 100, max_pages: int = 10
) -> list[GammaMarket]:
    """List active markets and filter to weather candidates.

    Raises on HTTP/network failure — the caller fails closed.
    """
    now = datetime.now(timezone.utc)
    results: list[GammaMarket] = []
    for page in range(max_pages):
        resp = client.get(
            f"{GAMMA_BASE}/markets",
            params={
                "closed": "false",
                "active": "true",
                "limit": page_size,
                "offset": page * page_size,
                "order": "volume24hr",
                "ascending": "false",
            },
            timeout=30,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            break
        for row in rows:
            try:
                market = GammaMarket.model_validate(row)
            except Exception:
                log.warning("gamma: unparseable market row id=%s", row.get("id"))
                continue
            market.fetched_at = now
            if market.active and not market.closed and looks_like_weather(market):
                results.append(market)
        if len(rows) < page_size:
            break
    log.info("gamma: %d weather candidates", len(results))
    return results
