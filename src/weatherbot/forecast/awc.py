"""Global METAR observations via aviationweather.gov (no key required).

Used for international stations whose Polymarket markets resolve against
the weather.gov timeseries page in whole degrees CELSIUS. That page's
"Temp" rows for these stations are the routine METAR reports, which METAR
encodes in whole degrees C — so every report counts (no per-hour selection
like the US 5-minute-data problem) and no unit conversion is involved.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from weatherbot.forecast.nws import ObservedDay
from weatherbot.forecast.stations import Station

log = logging.getLogger(__name__)

AWC_BASE = "https://aviationweather.gov/api/data/metar"


def _display_c(t: float) -> float:
    """Whole-degree display value (METARs are whole C; guard stray tenths)."""
    return float(math.floor(t + 0.5))


def reduce_reports(reports: list[dict], day: date, tz: ZoneInfo) -> ObservedDay | None:
    """Reduce raw METAR JSON reports to an ObservedDay for `day` local time.

    Returns None when no usable reports fall on the day."""
    temps: list[tuple[datetime, float]] = []
    last_obs: datetime | None = None
    for rep in reports:
        t = rep.get("temp")
        ts_raw = rep.get("reportTime") or rep.get("obsTime")
        if t is None or ts_raw is None:
            continue
        if isinstance(ts_raw, (int, float)):
            ts = datetime.fromtimestamp(ts_raw, tz=timezone.utc)
        else:
            ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        if last_obs is None or ts > last_obs:
            last_obs = ts
        if ts.astimezone(tz).date() != day:
            continue
        temps.append((ts, _display_c(float(t))))
    if not temps:
        return None
    temps.sort()
    hours_covered = {ts.astimezone(tz).hour for ts, _ in temps}
    values = [v for _, v in temps]
    return ObservedDay(
        station_id="",  # filled by caller
        day=day,
        high_f=max(values),   # NOTE: for awc stations these hold deg C —
        low_f=min(values),    # values are always in the station's display unit
        last_obs_at=last_obs,
        n_obs=len(hours_covered),
        current_f=temps[-1][1],
    )


def fetch_day_observations(
    client: httpx.Client, station: Station, day: date, user_agent: str
) -> ObservedDay | None:
    """All METAR reports for `day` in the station's local timezone.

    Returns None on fetch failure (caller fails closed)."""
    now_local_date = datetime.now(timezone.utc).astimezone(ZoneInfo(station.timezone)).date()
    hours_back = min(72, max(6, (now_local_date - day).days * 24 + 30))
    try:
        resp = client.get(
            AWC_BASE,
            params={"ids": station.station_id, "format": "json", "hours": hours_back},
            headers={"User-Agent": user_agent},
            timeout=30,
        )
        resp.raise_for_status()
        reports = resp.json()
        if not isinstance(reports, list):
            reports = []
    except Exception as exc:
        log.warning("awc metar fetch failed %s %s: %s", station.station_id, day, exc)
        return None

    obs = reduce_reports(reports, day, ZoneInfo(station.timezone))
    if obs is None:
        return ObservedDay(station.station_id, day, None, None, None, 0)
    obs.station_id = station.station_id
    return obs
