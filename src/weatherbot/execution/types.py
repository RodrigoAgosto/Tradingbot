"""Shared order-intent types for both executors."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class OrderIntent:
    market_id: str
    token_id: str | None
    side: str            # YES | NO (token being bought)
    price: float         # limit price = walked executable average
    shares: float
    cost_usd: float
    city: str | None = None
    station_id: str | None = None
    claim_json: str | None = None
    resolution_date: str | None = None

    def as_order_row(self, mode: str, status: str, detail: str | None = None) -> dict:
        return {
            "market_id": self.market_id,
            "token_id": self.token_id,
            "side": self.side,
            "action": "open",
            "price": self.price,
            "shares": self.shares,
            "cost_usd": self.cost_usd,
            "mode": mode,
            "status": status,
            "detail": detail,
        }


@dataclass
class CloseIntent:
    market_id: str
    token_id: str | None
    side: str
    price: float          # walked sell price
    shares: float
    cost_basis_usd: float
    reason: str = "edge_flip"


@dataclass
class ExecutionReport:
    ok: bool
    fill_price: float | None = None
    detail: str | None = None
