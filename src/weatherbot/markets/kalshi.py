"""Kalshi weather markets: discovery, claim construction, orderbooks.

Kalshi is the CFTC-regulated US venue — the one a US resident can legally
fund. Its API is far friendlier than Polymarket's for this job:

  * strikes are STRUCTURED (`strike_type` greater/less/between plus
    floor/cap) — no question parsing;
  * every market's rules text carries the resolution station as a climate
    product token, e.g. "(CLINYC)" = NYC Central Park — we map only tokens
    we have verified, and skip anything else (same discipline as the
    Polymarket parser);
  * orderbooks are public. Price/volume summary fields require auth, so
    the liquidity gate uses the book's total resting notional instead.

Settlement note: Kalshi settles on the official daily climate maximum
("according to The Weather Company", which reports the station's official
max). That is the continuous-sensor max, which can occasionally exceed the
hourly METAR max our observations track — calibration (keyed per station)
absorbs the systematic part, and paper settlement uses our observed value
as a proxy; the nightly review flags any resolution surprises.

Order placement (live) is a separate, authenticated step — not here.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone

import httpx

from weatherbot.forecast.stations import STATIONS
from weatherbot.markets.clob import BookLevel, OrderBook
from weatherbot.markets.parser import WeatherClaim

log = logging.getLogger(__name__)

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# Climate-product token (from rules_primary) -> our station id.
# ONLY verified mappings; an unknown token means skip, never guess.
CLI_TO_STATION: dict[str, str] = {
    "CLINYC": "KNYC",   # NYC Central Park
    "CLIMDW": "KMDW",   # Chicago Midway
    "CLILAX": "KLAX",   # Los Angeles Intl
    "CLIATL": "KATL",   # Atlanta Hartsfield
    "CLIMIA": "KMIA",   # Miami Intl
    "CLIAUS": "KAUS",   # Austin Bergstrom
    "CLISEA": "KSEA",   # Seattle-Tacoma
    "CLISFO": "KSFO",   # San Francisco Intl
    "CLIPHL": "KPHL",   # Philadelphia Intl
}

# Series we trade (high/low per city). Discovery still verifies each
# market's rules token against this map before trusting it.
SERIES: dict[str, str] = {
    "KXHIGHNY": "high_temp", "KXLOWNY": "low_temp",
    "KXHIGHCHI": "high_temp", "KXLOWCHI": "low_temp",
    "KXHIGHLAX": "high_temp", "KXLOWTLAX": "low_temp",
    "KXHIGHTATL": "high_temp",
    "KXHIGHMIA": "high_temp", "KXLOWMIA": "low_temp",
    "KXHIGHAUS": "high_temp", "KXLOWAUS": "low_temp",
    "KXHIGHTSEA": "high_temp",
    "KXHIGHTSFO": "high_temp",
    "KXHIGHPHIL": "high_temp", "KXLOWTPHIL": "low_temp",
}

_STATION_TOKEN_RE = re.compile(r"\((CLI[A-Z0-9]{2,5})\)")
_TICKER_DATE_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})-")
_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


@dataclass
class KalshiMarket:
    ticker: str
    title: str
    claim: WeatherClaim
    book_notional: float = 0.0   # resting $ posted on both sides
    close_time: datetime | None = None


def _ticker_date(ticker: str) -> date | None:
    m = _TICKER_DATE_RE.search(ticker)
    if not m:
        return None
    yy, mon, dd = m.groups()
    month = _MONTHS.get(mon)
    if not month:
        return None
    return date(2000 + int(yy), month, int(dd))


def parse_market(raw: dict, metric: str) -> tuple[KalshiMarket | None, str | None]:
    """Structured-strike market -> claim, or (None, skip_reason)."""
    ticker = raw.get("ticker", "")
    rules = raw.get("rules_primary") or ""
    tok = _STATION_TOKEN_RE.search(rules)
    if not tok:
        return None, "no_station_token_in_rules"
    station_id = CLI_TO_STATION.get(tok.group(1))
    if station_id is None:
        return None, f"station_not_in_allowlist:{tok.group(1)}"
    station = STATIONS[station_id]
    if "fahrenheit" not in rules.lower():
        return None, "unit_not_fahrenheit"

    res_date = _ticker_date(ticker)
    if res_date is None:
        return None, "resolution_date_unrecognized"

    st = raw.get("strike_type")
    floor, cap = raw.get("floor_strike"), raw.get("cap_strike")
    if st == "greater" and floor is not None:
        comparator, low, high = "above", float(floor), None
    elif st == "less" and cap is not None:
        # "less than 82" on integer settlement = 81 or below = value < 82
        comparator, low, high = "below", None, float(cap)
    elif st == "between" and floor is not None and cap is not None:
        # inclusive integer bucket 88-89 -> open interval (87.5, 89.5)
        comparator, low, high = "between", float(floor) - 0.5, float(cap) + 0.5
    else:
        return None, f"strike_unsupported:{st}"

    claim = WeatherClaim(
        market_id=ticker,
        city=station.city,
        station_id=station_id,
        metric=metric,  # type: ignore[arg-type]
        comparator=comparator,  # type: ignore[arg-type]
        threshold_low=low,
        threshold_high=high,
        unit="F",
        resolution_date=res_date,
        resolution_source=rules[:300],
    )
    close_raw = raw.get("close_time")
    close_time = None
    if close_raw:
        try:
            close_time = datetime.fromisoformat(str(close_raw).replace("Z", "+00:00"))
        except ValueError:
            pass
    return KalshiMarket(ticker=ticker, title=raw.get("title", ticker),
                        claim=claim, close_time=close_time), None


def fetch_markets(client: httpx.Client, cities: list[str]) -> tuple[list[KalshiMarket], list[tuple[str, str]]]:
    """Open markets across enabled series. Returns (markets, skips)."""
    wanted_series = {
        s: metric for s, metric in SERIES.items()
    }
    markets: list[KalshiMarket] = []
    skips: list[tuple[str, str]] = []
    for series, metric in wanted_series.items():
        try:
            resp = client.get(
                f"{KALSHI_BASE}/markets",
                params={"series_ticker": series, "status": "open", "limit": 100},
                timeout=30,
            )
            resp.raise_for_status()
            rows = resp.json().get("markets", [])
        except Exception as exc:
            log.warning("kalshi series fetch failed %s: %s", series, exc)
            skips.append((series, f"series_fetch_failed:{str(exc)[:60]}"))
            continue
        for raw in rows:
            market, reason = parse_market(raw, metric)
            if market is None:
                skips.append((raw.get("ticker", series), reason))
                continue
            if market.claim.city not in cities:
                skips.append((market.ticker, f"city_not_enabled:{market.claim.city}"))
                continue
            markets.append(market)
    log.info("kalshi: %d tradeable markets across %d series", len(markets), len(wanted_series))
    return markets, skips


def _ladder(levels: list) -> list[BookLevel]:
    out = []
    for price, qty in levels or []:
        try:
            out.append(BookLevel(float(price), float(qty)))
        except (TypeError, ValueError):
            continue
    return out


def fetch_books(client: httpx.Client, ticker: str) -> tuple[OrderBook, OrderBook, float]:
    """(yes_book, no_book, resting_notional_usd) for one market."""
    resp = client.get(f"{KALSHI_BASE}/markets/{ticker}/orderbook", timeout=15)
    resp.raise_for_status()
    ob = resp.json().get("orderbook_fp") or resp.json().get("orderbook") or {}
    return build_books(ob, ticker)


def build_books(ob: dict, ticker: str) -> tuple[OrderBook, OrderBook, float]:
    """Kalshi's book lists resting BIDS per side. Buying YES crosses the NO
    bids at price (1 - no_bid); buying NO crosses the YES bids likewise —
    so each side's executable asks are derived from the other side's bids,
    quantity preserved (contracts, $1 payout each).
    """
    yes_bids = _ladder(ob.get("yes_dollars") or ob.get("yes"))
    no_bids = _ladder(ob.get("no_dollars") or ob.get("no"))

    yes_asks = sorted((BookLevel(round(1.0 - l.price, 4), l.size) for l in no_bids),
                      key=lambda l: l.price)
    no_asks = sorted((BookLevel(round(1.0 - l.price, 4), l.size) for l in yes_bids),
                     key=lambda l: l.price)
    yes_bids.sort(key=lambda l: l.price, reverse=True)
    no_bids.sort(key=lambda l: l.price, reverse=True)

    notional = sum(l.price * l.size for l in yes_bids) + sum(l.price * l.size for l in no_bids)
    yes_book = OrderBook(token_id=f"{ticker}:YES", bids=yes_bids, asks=yes_asks)
    no_book = OrderBook(token_id=f"{ticker}:NO", bids=no_bids, asks=no_asks)
    return yes_book, no_book, notional
