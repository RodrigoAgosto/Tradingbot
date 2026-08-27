"""Entry / exit / skip decision logic.

Entry requires ALL conditions; the first failed check becomes the recorded
skip reason so the DB explains every non-trade.
"""

from __future__ import annotations

from dataclasses import dataclass

from weatherbot.config import StrategyConfig
from weatherbot.strategy.edge import EdgeResult


@dataclass
class EntryContext:
    edge_result: EdgeResult
    confidence: float
    lead_days: float
    volume_24h: float
    has_position: bool
    observed_decided: bool = False  # observation has already settled the market


def entry_skip_reason(ctx: EntryContext, cfg: StrategyConfig) -> str | None:
    """None means all entry conditions pass."""
    er = ctx.edge_result
    if ctx.has_position:
        return "already_in_position"
    if ctx.lead_days > cfg.max_lead_days:
        return f"lead_too_long:{ctx.lead_days:.1f}d>{cfg.max_lead_days}d"
    if ctx.volume_24h < cfg.min_volume_24h_usd:
        return f"volume_too_low:{ctx.volume_24h:.0f}<{cfg.min_volume_24h_usd:.0f}"
    if ctx.confidence < cfg.min_confidence:
        return f"confidence_too_low:{ctx.confidence:.2f}<{cfg.min_confidence:.2f}"
    if er.edge < cfg.min_edge:
        return f"edge_too_small:{er.edge:.3f}<{cfg.min_edge:.3f}"
    if er.edge > cfg.max_edge and not ctx.observed_decided:
        # a liquid market disagreeing with us this much means our data is
        # probably wrong; only a settled observation justifies taking it
        return f"edge_implausibly_large:{er.edge:.3f}>{cfg.max_edge:.3f}"
    if not er.depth_filled:
        return "insufficient_depth"
    if er.slippage > cfg.max_slippage:
        return f"slippage_too_high:{er.slippage:.3f}>{cfg.max_slippage:.3f}"
    if er.exec_price <= 0.01 or er.exec_price >= 0.99:
        return "price_at_bound"
    return None


def rank_key(ctx: EntryContext) -> float:
    """Surviving candidates are ranked by edge * confidence, best first."""
    return ctx.edge_result.edge * ctx.confidence


def should_exit(fair_side_prob: float, sell_price: float | None, exit_edge: float) -> bool:
    """Early exit when the forecast moved decisively against the position:
    the fair value of what we hold sits `exit_edge` BELOW what the market
    will pay us for it right now.
    """
    if sell_price is None:
        return False
    return (fair_side_prob - sell_price) <= -exit_edge
