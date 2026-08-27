"""NWS observations for resolution-relevant stations.

Used for (a) same-day markets where the running observed high dominates the
forecast, and (b) settling paper positions / filling calibration actuals
once a day is complete.

CRITICAL — match the resolution source exactly. Polymarket weather markets
resolve off the weather.gov timeseries page's HOURLY rows ("Show Hourly
Data"), in whole degrees Fahrenheit. The NWS API also serves 5-minute
observations in whole degrees CELSIUS; converting those to F overstates
the reading by up to ~0.9F (26C -> 78.8F when the page shows 77-78) and
can falsely bust a 2-degree bucket. So we:
  * keep only the routine hourly METAR (the observation nearest minute :51
    of each hour, which carries tenth-degree-C precision), and
  * round the converted value to whole degrees F, the page's precision.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from weatherbot.forecast.stations import Station

log = logging.getLogger(__name__)

NWS_BASE = "https://api.weather.gov"


METAR_MINUTE = 51        # routine hourly METAR time at KLGA / KORD
METAR_WINDOW_START = 44  # accept obs in [:44, :59] as "the hourly reading"


@dataclass
class ObservedDay:
    station_id: str
    day: date
    high_f: float | None   # whole degrees F, hourly readings only
    low_f: float | None
    last_obs_at: datetime | None
    n_obs: int             # number of hourly readings seen
    current_f: float | None = None  # most recent hourly reading, whole F

    @property
    def complete(self) -> bool:
        """True when the local day has ended and we saw most of its hours."""
        if self.last_obs_at is None:
            return False
        return self.n_obs >= 20


def _c_to_display_f(c: float) -> float:
    """Whole-degree F the resolution page displays for a Celsius reading."""
    import math

    return float(math.floor(c * 9.0 / 5.0 + 32.0 + 0.5))


def fetch_day_observations(
    client: httpx.Client, station: Station, day: date, user_agent: str
) -> ObservedDay | None:
    """All observations for `day` in the station's local timezone.

    Returns None on fetch failure (caller fails closed for same-day logic).
    """
    tz = ZoneInfo(station.timezone)
    start_local = datetime.combine(day, time.min, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    try:
        resp = client.get(
            f"{NWS_BASE}/stations/{station.station_id}/observations",
            params={
                "start": start_local.astimezone(timezone.utc).isoformat(timespec="seconds"),
                "end": end_local.astimezone(timezone.utc).isoformat(timespec="seconds"),
                "limit": 500,
            },
            headers={"User-Agent": user_agent, "Accept": "application/geo+json"},
            timeout=30,
        )
        resp.raise_for_status()
        features = resp.json().get("features", [])
    except Exception as exc:
        log.warning("nws observations fetch failed %s %s: %s", station.station_id, day, exc)
        return None

    temps_f, last_obs, current_f = select_hourly_temps(features)
    if not temps_f:
        return ObservedDay(station.station_id, day, None, None, last_obs, 0)
    return ObservedDay(
        station_id=station.station_id,
        day=day,
        high_f=max(temps_f),
        low_f=min(temps_f),
        last_obs_at=last_obs,
        n_obs=len(temps_f),
        current_f=current_f,
    )


def select_hourly_temps(
    features: list[dict],
) -> tuple[list[float], datetime | None, float | None]:
    """One display-F reading per hour: the observation nearest minute :51
    within [:44, :59] — the routine hourly METAR the resolution page shows.
    Whole-degree-C 5-minute obs outside that window are ignored; they carry
    up to ~0.9F of conversion error.

    Returns (hourly display-F temps, latest obs time seen, latest hourly F).
    """
    hourly: dict[tuple, tuple[int, float, datetime]] = {}  # hour-key -> (dist, temp_c, ts)
    last_obs: datetime | None = None
    for feat in features:
        props = feat.get("properties", {})
        temp = (props.get("temperature") or {}).get("value")
        qc = (props.get("temperature") or {}).get("qualityControl", "")
        if temp is None or qc in ("Z", "X"):
            continue
        ts_raw = props.get("timestamp")
        if not ts_raw:
            continue
        ts = datetime.fromisoformat(ts_raw)
        if last_obs is None or ts > last_obs:
            last_obs = ts
        if ts.minute < METAR_WINDOW_START:
            continue
        key = (ts.date(), ts.hour)
        dist = abs(ts.minute - METAR_MINUTE)
        if key not in hourly or dist < hourly[key][0]:
            hourly[key] = (dist, float(temp), ts)
    if not hourly:
        return [], last_obs, None
    entries = list(hourly.values())
    latest = max(entries, key=lambda e: e[2])
    return [_c_to_display_f(tc) for _, tc, _ in entries], last_obs, _c_to_display_f(latest[1])


def local_now(station: Station, now_utc: datetime | None = None) -> datetime:
    now_utc = now_utc or datetime.now(timezone.utc)
    return now_utc.astimezone(ZoneInfo(station.timezone))
