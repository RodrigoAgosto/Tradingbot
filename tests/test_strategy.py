"""Orderbook walking, edge, sizing, entry/exit rules."""

from weatherbot.config import StrategyConfig
from weatherbot.markets.clob import BookLevel, OrderBook, walk_buy, walk_sell
from weatherbot.strategy.edge import compute_edge
from weatherbot.strategy.rules import EntryContext, entry_skip_reason, should_exit
from weatherbot.strategy.sizing import kelly_fraction, size_position


def yes_book(levels):
    return OrderBook(token_id="yes", asks=[BookLevel(p, s) for p, s in levels])


def test_walk_buy_averages_across_levels():
    book = yes_book([(0.50, 10), (0.60, 100)])  # $5 at .50 then .60
    res = walk_buy(book, 11.0)
    assert res.filled
    # $5 buys 10 sh @ .50, remaining $6 buys 10 sh @ .60 -> avg .55
    assert abs(res.avg_price - 0.55) < 1e-9
    assert abs(res.shares - 20.0) < 1e-9


def test_walk_buy_insufficient_depth():
    res = walk_buy(yes_book([(0.5, 4)]), 100.0)
    assert not res.filled


def test_walk_sell():
    book = OrderBook(token_id="t", bids=[BookLevel(0.6, 10), BookLevel(0.5, 10)])
    res = walk_sell(book, 15)
    assert res.filled
    assert abs(res.avg_price - ((10 * 0.6 + 5 * 0.5) / 15)) < 1e-9


def test_edge_uses_executable_not_best():
    # best ask 0.50 but only $1 deep; the rest at 0.70
    book = yes_book([(0.50, 2), (0.70, 1000)])
    er = compute_edge(0.60, book, None, "yes", None, target_usd=50.0)
    assert er.side == "YES"
    assert er.exec_price > 0.69  # nearly all fills at .70
    assert er.edge < 0  # a "10c edge on the best ask" that is negative for real size


def test_edge_picks_no_side():
    yb = yes_book([(0.80, 1000)])
    nb = OrderBook(token_id="no", asks=[BookLevel(0.25, 1000)])
    er = compute_edge(0.60, yb, nb, "yes", "no", target_usd=20.0)
    assert er.side == "NO"
    assert abs(er.fair_side_prob - 0.40) < 1e-9
    assert abs(er.edge - 0.15) < 1e-9


def test_kelly_sizing():
    assert abs(kelly_fraction(0.10, 0.50) - 0.20) < 1e-9
    sized = size_position(1000.0, 0.10, 0.50, kelly_multiplier=0.25,
                          max_position_frac=0.05, available_depth_usd=1e9)
    # kelly 0.2 * 0.25 = 5% == cap -> $50
    assert abs(sized.cost_usd - 50.0) < 1e-9
    assert abs(sized.shares - 100.0) < 1e-9


def test_sizing_respects_cap_and_depth():
    sized = size_position(1000.0, 0.30, 0.50, 0.25, 0.05, available_depth_usd=20.0)
    assert sized.cost_usd == 20.0
    assert size_position(1000.0, -0.05, 0.50, 0.25, 0.05, 1e9) is None
    assert size_position(10.0, 0.02, 0.5, 0.25, 0.05, 1e9) is None  # < $1


def _ctx(**over):
    book = yes_book([(0.50, 1000)])
    er = compute_edge(0.62, book, None, "yes", None, 20.0)
    base = dict(edge_result=er, confidence=0.8, lead_days=1.0,
                volume_24h=10000.0, has_position=False)
    base.update(over)
    return EntryContext(**base)


def test_entry_all_conditions_pass():
    assert entry_skip_reason(_ctx(), StrategyConfig()) is None


def test_entry_rejections():
    cfg = StrategyConfig()
    assert "already_in_position" in entry_skip_reason(_ctx(has_position=True), cfg)
    assert "lead_too_long" in entry_skip_reason(_ctx(lead_days=5.0), cfg)
    assert "volume_too_low" in entry_skip_reason(_ctx(volume_24h=100.0), cfg)
    assert "confidence_too_low" in entry_skip_reason(_ctx(confidence=0.3), cfg)


def test_entry_min_edge():
    book = yes_book([(0.58, 1000)])
    er = compute_edge(0.62, book, None, "yes", None, 20.0)  # edge 0.04 < 0.08
    ctx = _ctx(edge_result=er)
    assert "edge_too_small" in entry_skip_reason(ctx, StrategyConfig())


def test_entry_slippage_gate():
    book = yes_book([(0.30, 2), (0.40, 1000)])  # heavy slippage from best
    er = compute_edge(0.62, book, None, "yes", None, 50.0)
    ctx = _ctx(edge_result=er)
    assert "slippage_too_high" in entry_skip_reason(ctx, StrategyConfig())


def test_entry_implausible_edge_gate():
    cfg = StrategyConfig()
    book = yes_book([(0.10, 10000)])
    er = compute_edge(0.90, book, None, "yes", None, 20.0)  # 0.80 "edge"
    assert "edge_implausibly_large" in entry_skip_reason(_ctx(edge_result=er), cfg)
    # ...unless the observation has already settled the outcome
    assert entry_skip_reason(_ctx(edge_result=er, observed_decided=True), cfg) is None


def test_exit_on_edge_flip():
    # hold YES bought at .55; fair now .40, market pays .52 -> flip > .10
    assert should_exit(fair_side_prob=0.40, sell_price=0.52, exit_edge=0.10)
    assert not should_exit(fair_side_prob=0.48, sell_price=0.52, exit_edge=0.10)
    assert not should_exit(0.40, None, 0.10)
