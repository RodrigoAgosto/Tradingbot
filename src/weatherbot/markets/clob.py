"""Polymarket CLOB public market data: orderbooks and executable prices.

Order *placement* lives in execution/live.py. This module is read-only and
unauthenticated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)

CLOB_BASE = "https://clob.polymarket.com"


@dataclass
class BookLevel:
    price: float
    size: float  # shares


@dataclass
class OrderBook:
    token_id: str
    bids: list[BookLevel] = field(default_factory=list)  # sorted best (highest) first
    asks: list[BookLevel] = field(default_factory=list)  # sorted best (lowest) first
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def age_seconds(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        return (now - self.fetched_at).total_seconds()

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None


@dataclass
class WalkResult:
    """Result of walking one side of the book for a target notional."""
    avg_price: float
    shares: float
    cost_usd: float
    filled: bool  # whole target notional available


def fetch_book(client: httpx.Client, token_id: str) -> OrderBook:
    resp = client.get(f"{CLOB_BASE}/book", params={"token_id": token_id}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    bids = sorted(
        (BookLevel(float(l["price"]), float(l["size"])) for l in data.get("bids", [])),
        key=lambda l: l.price,
        reverse=True,
    )
    asks = sorted(
        (BookLevel(float(l["price"]), float(l["size"])) for l in data.get("asks", [])),
        key=lambda l: l.price,
    )
    return OrderBook(token_id=token_id, bids=bids, asks=asks)


def walk_buy(book: OrderBook, target_usd: float) -> WalkResult | None:
    """Average fill price for buying `target_usd` notional against the asks.

    Returns None when the book is empty. `filled` is False when depth runs
    out before the target notional is reached (result covers what IS there).
    """
    if not book.asks or target_usd <= 0:
        return None
    remaining = target_usd
    shares = 0.0
    cost = 0.0
    for level in book.asks:
        level_cost = level.price * level.size
        take = min(remaining, level_cost)
        shares += take / level.price
        cost += take
        remaining -= take
        if remaining <= 1e-9:
            break
    if shares <= 0:
        return None
    return WalkResult(avg_price=cost / shares, shares=shares, cost_usd=cost, filled=remaining <= 1e-9)


def walk_sell(book: OrderBook, shares_to_sell: float) -> WalkResult | None:
    """Average fill price for selling `shares_to_sell` into the bids."""
    if not book.bids or shares_to_sell <= 0:
        return None
    remaining = shares_to_sell
    proceeds = 0.0
    sold = 0.0
    for level in book.bids:
        take = min(remaining, level.size)
        proceeds += take * level.price
        sold += take
        remaining -= take
        if remaining <= 1e-9:
            break
    if sold <= 0:
        return None
    return WalkResult(avg_price=proceeds / sold, shares=sold, cost_usd=proceeds, filled=remaining <= 1e-9)
