"""Stale data must be rejected: forecasts > 4 h, market data > 60 s."""

from datetime import datetime, timedelta, timezone

from weatherbot import db
from weatherbot.forecast.openmeteo import EnsembleData
from weatherbot.markets.clob import BookLevel, OrderBook


def test_forecast_stale_after_4_hours():
    old = datetime.now(timezone.utc) - timedelta(hours=5)
    data = EnsembleData(station_id="KLGA", fetched_at=old, daily={})
    assert data.age_hours() > 4.0


def test_forecast_fresh_within_4_hours():
    recent = datetime.now(timezone.utc) - timedelta(hours=1)
    data = EnsembleData(station_id="KLGA", fetched_at=recent, daily={})
    assert data.age_hours() <= 4.0


def test_orderbook_age():
    book = OrderBook(token_id="t", asks=[BookLevel(0.5, 100)],
                     fetched_at=datetime.now(timezone.utc) - timedelta(seconds=90))
    assert book.age_seconds() > 60
    fresh = OrderBook(token_id="t", asks=[BookLevel(0.5, 100)])
    assert fresh.age_seconds() < 60


def test_cache_expiry(conn):
    db.cache_put(conn, "k1", "KLGA", {"x": 1})
    assert db.cache_get(conn, "k1", max_age_seconds=3600) is not None
    # simulate old entry
    old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(timespec="seconds")
    conn.execute("UPDATE forecast_cache SET fetched_at = ? WHERE cache_key = 'k1'", (old,))
    conn.commit()
    assert db.cache_get(conn, "k1", max_age_seconds=4 * 3600) is None
