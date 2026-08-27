"""Position sizing: fractional Kelly with a hard per-position cap.

    kelly_fraction = edge / (1 - price)      # for buying a token at `price`
    size_usd       = bankroll * kelly_fraction * kelly_multiplier

Full Kelly on a mismeasured edge is how accounts die; kelly_multiplier
defaults to 0.25. risk.py re-checks the final size against every cap
immediately before the order is routed.
"""

from __future__ import annotations

from dataclasses import dataclass

MIN_ORDER_USD = 1.0


@dataclass
class SizedOrder:
    cost_usd: float
    shares: float
    price: float


def kelly_fraction(edge: float, price: float) -> float:
    if price >= 1.0 or edge <= 0:
        return 0.0
    return edge / (1.0 - price)


def size_position(
    bankroll: float,
    edge: float,
    exec_price: float,
    kelly_multiplier: float,
    max_position_frac: float,
    available_depth_usd: float,
) -> SizedOrder | None:
    """Returns None when the sized order is too small to bother with."""
    frac = kelly_fraction(edge, exec_price) * kelly_multiplier
    if frac <= 0:
        return None
    cost = bankroll * frac
    cost = min(cost, bankroll * max_position_frac, available_depth_usd)
    if cost < MIN_ORDER_USD:
        return None
    return SizedOrder(cost_usd=cost, shares=cost / exec_price, price=exec_price)
