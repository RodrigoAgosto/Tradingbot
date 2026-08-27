"""Open-Meteo ensemble forecasts (free, no key).

Fetches all individual ensemble members (GFS ensemble + ECMWF IFS ensemble)
for a station's coordinates and reduces hourly member series to per-member
daily highs/lows in the station's local timezone.

Caching: raw responses are cached in SQLite for `cache_minutes` (default 30).
If a refetch fails, cached data younger than the staleness limit (4 h) is
still usable — older than that, the station has NO forecast this cycle and
every market depending on it is skipped (fail closed).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone

import httpx

from weatherbot import db
from weatherbot.forecast.stations import Station

log = logging.getLogger(__name__)

ENSEMBLE_BASE = "https://ensemble-api.open-meteo.com/v1/ensemble"


@dataclass
class EnsembleData:
    station_id: str
    fetched_at: datetime
    # ISO day -> {"high": [per-member daily max F], "low": [per-member daily min F]}
    daily: dict[str, dict[str, list[float]]]

    def age_hours(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        return (now - self.fetched_at).total_seconds() / 3600.0

    def members_for(self, day: date, metric: str) -> list[float]:
        key = "high" if metric == "high_temp" else "low"
        return self.daily.get(day.isoformat(), {}).get(key, [])

    def now_for(self, day: date) -> list[float]:
        """Per-member forecast for the current hour (today only), aligned
        index-for-index with members_for. Empty for future days."""
        return self.daily.get(day.isoformat(), {}).get("now", [])


def _member_series(hourly: dict) -> dict[str, list]:
    """All hourly temperature member series in the response."""
    return {
        k: v
        for k, v in hourly.items()
        if k.startswith("temperature_2m") and isinstance(v, list)
    }


def _reduce_daily(
    payloads: list[dict],
    today: str | None = None,
    from_hour: int | None = None,
) -> dict[str, dict[str, list[float]]]:
    """Reduce hourly member series (already in station-local time) to per-member
    daily max/min. Members from all models are pooled into one ensemble.

    For `today`, only hours >= `from_hour` are used: the result is each
    member's REMAINING-day extreme. Combined with the observed running
    high/low, that is the correct same-day distribution — the full-day
    forecast max is stale once part of the day is observed.
    """
    daily: dict[str, dict[str, list[float]]] = {}
    for payload in payloads:
        hourly = payload.get("hourly", {})
        times = hourly.get("time", [])
        for _, series in sorted(_member_series(hourly).items()):
            per_day: dict[str, list[float]] = {}
            now_val: float | None = None
            for t, v in zip(times, series):
                if v is None:
                    continue
                day = t[:10]
                if day == today and from_hour is not None:
                    hour = int(t[11:13])
                    if hour == from_hour:
                        now_val = float(v)
                    if hour < from_hour:
                        continue
                per_day.setdefault(day, []).append(float(v))
            for day, vals in per_day.items():
                if day != today and len(vals) < 18:
                    continue  # require most of a future day for a usable extreme
                bucket = daily.setdefault(day, {"high": [], "low": [], "now": []})
                bucket["high"].append(max(vals))
                bucket["low"].append(min(vals))
                if day == today:
                    # this member's forecast for the CURRENT hour, used to
                    # anchor remaining-day values to the live observation
                    bucket["now"].append(now_val if now_val is not None else vals[0])
    return daily


def _fetch_model(client: httpx.Client, station: Station, model: str, forecast_days: int) -> dict:
    resp = client.get(
        ENSEMBLE_BASE,
        params={
            "latitude": station.lat,
            "longitude": station.lon,
            "hourly": "temperature_2m",
            "models": model,
            "forecast_days": forecast_days,
            "timezone": station.timezone,
            "temperature_unit": "fahrenheit",
        },
        timeout=45,
    )
    resp.raise_for_status()
    return resp.json()


def get_ensemble(
    conn,
    client: httpx.Client,
    station: Station,
    models: list[str],
    cache_minutes: int,
    stale_hours: float,
    forecast_days: int = 7,
    today_local: str | None = None,
    from_hour: int | None = None,
) -> EnsembleData | None:
    """Cached ensemble fetch. Returns None when no non-stale data exists.

    today_local/from_hour: station-local current day and hour; today's
    member extremes are computed over the remaining hours only.
    """
    payloads: list[dict] = []
    oldest_fetch: datetime | None = None

    for model in models:
        key = f"openmeteo:{station.station_id}:{model}"
        cached = db.cache_get(conn, key, cache_minutes * 60)
        if cached is None:
            try:
                payload = _fetch_model(client, station, model, forecast_days)
                db.cache_put(conn, key, station.station_id, payload)
                cached = {"fetched_at": db.utcnow(), "payload": payload}
            except Exception as exc:
                log.warning("openmeteo fetch failed station=%s model=%s: %s",
                            station.station_id, model, exc)
                # fall back to cache within the staleness window
                cached = db.cache_get(conn, key, stale_hours * 3600)
                if cached is None:
                    continue
                log.warning("openmeteo using aged cache for %s/%s", station.station_id, model)
        payloads.append(cached["payload"])
        fetched = datetime.fromisoformat(cached["fetched_at"])
        if oldest_fetch is None or fetched < oldest_fetch:
            oldest_fetch = fetched

    if not payloads or oldest_fetch is None:
        return None

    data = EnsembleData(
        station_id=station.station_id,
        fetched_at=oldest_fetch,
        daily=_reduce_daily(payloads, today=today_local, from_hour=from_hour),
    )
    if data.age_hours() > stale_hours:
        log.warning("openmeteo data stale for %s (%.1f h)", station.station_id, data.age_hours())
        return None
    return data
