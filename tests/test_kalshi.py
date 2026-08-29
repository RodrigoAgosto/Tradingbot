"""Kalshi venue adapter: structured strikes -> claims, book transformation."""

from weatherbot.markets.clob import walk_buy
from weatherbot.markets.kalshi import build_books, parse_market


def raw(strike_type, floor=None, cap=None, station="CLINYC", ticker="KXHIGHNY-26AUG30-T89"):
    return {
        "ticker": ticker,
        "title": "Will the maximum temperature be >89° on Aug 30, 2026?",
        "strike_type": strike_type,
        "floor_strike": floor,
        "cap_strike": cap,
        "close_time": "2026-08-31T05:00:00Z",
        "rules_primary": f"If the maximum temperature recorded at New York City ({station}) "
                         "for Aug 30, 2026, is greater than 89° fahrenheit according to "
                         "The Weather Company, then the market resolves to Yes.",
    }


def test_parse_greater_strike():
    m, reason = parse_market(raw("greater", floor=89), "high_temp")
    assert m is not None, reason
    c = m.claim
    assert c.station_id == "KNYC" and c.city == "New York"
    assert c.comparator == "above" and c.threshold_low == 89.0
    assert c.resolution_date.isoformat() == "2026-08-30"
    assert c.unit == "F"


def test_parse_less_strike():
    m, reason = parse_market(raw("less", cap=82, ticker="KXHIGHNY-26AUG30-T82"), "high_temp")
    assert m is not None, reason
    assert m.claim.comparator == "below" and m.claim.threshold_high == 82.0


def test_parse_between_strike():
    m, reason = parse_market(raw("between", floor=88, cap=89,
                                 ticker="KXHIGHNY-26AUG30-B88.5"), "high_temp")
    assert m is not None, reason
    assert m.claim.comparator == "between"
    assert m.claim.threshold_low == 87.5 and m.claim.threshold_high == 89.5


def test_parse_unknown_station_skipped():
    m, reason = parse_market(raw("greater", floor=89, station="CLIDEN"), "high_temp")
    assert m is None and "station_not_in_allowlist:CLIDEN" in reason


def test_parse_no_station_token_skipped():
    r = raw("greater", floor=89)
    r["rules_primary"] = "resolves according to The Weather Company."
    m, reason = parse_market(r, "high_temp")
    assert m is None and reason == "no_station_token_in_rules"


def test_build_books_derives_asks_from_opposite_bids():
    ob = {
        "yes_dollars": [["0.0100", "50.00"], ["0.0500", "100.00"]],
        "no_dollars": [["0.9000", "200.00"], ["0.9500", "300.00"]],
    }
    yes_book, no_book, notional = build_books(ob, "T")
    # buying YES crosses best NO bid 0.95 -> ask 0.05
    assert yes_book.best_ask == 0.05
    assert yes_book.best_bid == 0.05  # best yes bid
    assert no_book.best_ask == 0.95  # 1 - best yes bid 0.05
    walk = walk_buy(yes_book, 10.0)  # $10 at 0.05 = 200 contracts, all there
    assert walk.filled and abs(walk.avg_price - 0.05) < 1e-9
    # notional: 0.01*50 + 0.05*100 + 0.9*200 + 0.95*300 = 0.5+5+180+285
    assert abs(notional - 470.5) < 1e-6


def test_kalshi_liquidity_gate_uses_venue_floor():
    from weatherbot.config import StrategyConfig
    from weatherbot.markets.clob import BookLevel, OrderBook
    from weatherbot.strategy.edge import compute_edge
    from weatherbot.strategy.rules import EntryContext, entry_skip_reason

    cfg = StrategyConfig()
    book = OrderBook(token_id="t", asks=[BookLevel(0.50, 1000)])
    er = compute_edge(0.62, book, None, "t", None, 20.0)
    base = dict(edge_result=er, confidence=0.8, lead_days=1.0, has_position=False)
    # $2k book: fails the polymarket default ($5k) but passes the kalshi floor ($1k)
    assert "volume_too_low" in entry_skip_reason(
        EntryContext(**base, volume_24h=2000.0), cfg)
    assert entry_skip_reason(
        EntryContext(**base, volume_24h=2000.0, min_volume=cfg.kalshi_min_book_usd), cfg) is None
