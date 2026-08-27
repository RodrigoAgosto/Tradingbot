"""Edge: fair probability vs the EXECUTABLE price (never the midpoint).

We walk the orderbook for the size we intend to trade and use the real
average fill price. Both sides are considered: buying YES when the market
underprices the event, buying NO when it overprices it.
"""

from __future__ import annotations

from dataclasses import dataclass

from weatherbot.markets.clob import OrderBook, WalkResult, walk_buy


@dataclass
class EdgeResult:
    side: str                # "YES" or "NO" — token we would buy
    token_id: str | None
    fair_side_prob: float    # probability the bought token pays $1
    exec_price: float        # avg fill for the target notional
    best_price: float        # best ask before walking
    edge: float              # fair_side_prob - exec_price
    slippage: float          # exec_price - best_price
    depth_filled: bool       # book had the whole target notional
    walk: WalkResult
    market_implied_yes: float  # market's implied P(YES) at executable prices


def compute_edge(
    fair_p_yes: float,
    yes_book: OrderBook | None,
    no_book: OrderBook | None,
    yes_token: str | None,
    no_token: str | None,
    target_usd: float,
) -> EdgeResult | None:
    """Best positive-edge side for `target_usd` notional, or None if neither
    book is walkable."""
    candidates: list[EdgeResult] = []

    if yes_book is not None and yes_book.asks:
        walk = walk_buy(yes_book, target_usd)
        if walk is not None:
            candidates.append(
                EdgeResult(
                    side="YES",
                    token_id=yes_token,
                    fair_side_prob=fair_p_yes,
                    exec_price=walk.avg_price,
                    best_price=yes_book.best_ask,
                    edge=fair_p_yes - walk.avg_price,
                    slippage=walk.avg_price - yes_book.best_ask,
                    depth_filled=walk.filled,
                    walk=walk,
                    market_implied_yes=walk.avg_price,
                )
            )

    if no_book is not None and no_book.asks:
        walk = walk_buy(no_book, target_usd)
        if walk is not None:
            fair_no = 1.0 - fair_p_yes
            candidates.append(
                EdgeResult(
                    side="NO",
                    token_id=no_token,
                    fair_side_prob=fair_no,
                    exec_price=walk.avg_price,
                    best_price=no_book.best_ask,
                    edge=fair_no - walk.avg_price,
                    slippage=walk.avg_price - no_book.best_ask,
                    depth_filled=walk.filled,
                    walk=walk,
                    market_implied_yes=1.0 - walk.avg_price,
                )
            )

    if not candidates:
        return None
    return max(candidates, key=lambda c: c.edge)
