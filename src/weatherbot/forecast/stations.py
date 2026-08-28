"""Explicit allowlist of supported cities/stations.

A market that cannot be mapped to one of these stations is skipped —
unknown city means skip, always.

Two observation adapters:
  * "nws": api.weather.gov, US (and some Canadian) stations, deg F display.
  * "awc": aviationweather.gov global METARs, deg C display — the same
    reports the weather.gov timeseries resolution pages show for
    international stations.

NOT supported: Taipei and Hong Kong. Their Polymarket rules carry no
weather.gov timeseries URL — they resolve directly against CWA / HKO — so
the parser skips them (no_station_in_resolution_rules), correctly.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Station:
    station_id: str
    city: str
    name: str
    lat: float
    lon: float
    timezone: str
    source: str  # observation adapter: "nws" (US/Canada) or "awc" (global METAR)
    unit: str = "F"  # unit the resolution source displays: "F" or "C"


# Verified against live Polymarket resolution rules (2026-08): NYC markets
# resolve at LaGuardia (weather.gov/wrh/timeseries?site=klga), Chicago at
# O'Hare (site=kord). NOT Central Park / Midway — always trust the rules text.
STATIONS: dict[str, Station] = {
    "KLGA": Station(
        station_id="KLGA",
        city="New York",
        name="New York LaGuardia Airport",
        lat=40.7794,
        lon=-73.8803,
        timezone="America/New_York",
        source="nws",
    ),
    "KORD": Station(
        station_id="KORD",
        city="Chicago",
        name="Chicago O'Hare Intl Airport",
        lat=41.9786,
        lon=-87.9048,
        timezone="America/Chicago",
        source="nws",
    ),
    "KLAX": Station(
        station_id="KLAX",
        city="Los Angeles",
        name="Los Angeles Intl Airport",
        lat=33.9382,
        lon=-118.3866,
        timezone="America/Los_Angeles",
        source="nws",
    ),
    "KHOU": Station(
        station_id="KHOU",
        city="Houston",
        name="Houston Hobby Airport",
        lat=29.6454,
        lon=-95.2789,
        timezone="America/Chicago",
        source="nws",
    ),
    "KMIA": Station(
        station_id="KMIA",
        city="Miami",
        name="Miami Intl Airport",
        lat=25.7906,
        lon=-80.3164,
        timezone="America/New_York",
        source="nws",
    ),
    "KATL": Station(
        station_id="KATL",
        city="Atlanta",
        name="Atlanta Hartsfield-Jackson Intl Airport",
        lat=33.6301,
        lon=-84.4418,
        timezone="America/New_York",
        source="nws",
    ),
    "KDAL": Station(
        station_id="KDAL",
        city="Dallas",
        name="Dallas Love Field",
        lat=32.8471,
        lon=-96.8518,
        timezone="America/Chicago",
        source="nws",
    ),
    "KSFO": Station(
        station_id="KSFO",
        city="San Francisco",
        name="San Francisco Intl Airport",
        lat=37.6188,
        lon=-122.3754,
        timezone="America/Los_Angeles",
        source="nws",
    ),
    "KAUS": Station(
        station_id="KAUS",
        city="Austin",
        name="Austin-Bergstrom Intl Airport",
        lat=30.1831,
        lon=-97.6799,
        timezone="America/Chicago",
        source="nws",
    ),
    "KSEA": Station(
        station_id="KSEA",
        city="Seattle",
        name="Seattle-Tacoma Intl Airport",
        lat=47.4489,
        lon=-122.3094,
        timezone="America/Los_Angeles",
        source="nws",
    ),
    "KBKF": Station(
        station_id="KBKF",
        city="Denver",
        name="Buckley Space Force Base",
        lat=39.7017,
        lon=-104.7517,
        timezone="America/Denver",
        source="nws",
    ),
    # --- international (deg C markets, global METAR observations) ---
    "SAEZ": Station(
        station_id="SAEZ",
        city="Buenos Aires",
        name="Buenos Aires Ezeiza Intl Airport",
        lat=-34.8222,
        lon=-58.5358,
        timezone="America/Argentina/Buenos_Aires",
        source="awc",
        unit="C",
    ),
    "SBGR": Station(
        station_id="SBGR",
        city="Sao Paulo",
        name="Sao Paulo Guarulhos Intl Airport",
        lat=-23.4356,
        lon=-46.4731,
        timezone="America/Sao_Paulo",
        source="awc",
        unit="C",
    ),
    "EGLC": Station(
        station_id="EGLC",
        city="London",
        name="London City Airport",
        lat=51.5053,
        lon=0.0553,
        timezone="Europe/London",
        source="awc",
        unit="C",
    ),
    "LFPB": Station(
        station_id="LFPB",
        city="Paris",
        name="Paris Le Bourget Airport",
        lat=48.9694,
        lon=2.4414,
        timezone="Europe/Paris",
        source="awc",
        unit="C",
    ),
    "EDDM": Station(
        station_id="EDDM",
        city="Munich",
        name="Munich Airport",
        lat=48.3538,
        lon=11.7861,
        timezone="Europe/Berlin",
        source="awc",
        unit="C",
    ),
    "LTAC": Station(
        station_id="LTAC",
        city="Ankara",
        name="Ankara Esenboga Airport",
        lat=40.1281,
        lon=32.9951,
        timezone="Europe/Istanbul",
        source="awc",
        unit="C",
    ),
    "LLBG": Station(
        station_id="LLBG",
        city="Tel Aviv",
        name="Tel Aviv Ben Gurion Airport",
        lat=32.0114,
        lon=34.8867,
        timezone="Asia/Jerusalem",
        source="awc",
        unit="C",
    ),
    "ZBAA": Station(
        station_id="ZBAA",
        city="Beijing",
        name="Beijing Capital Intl Airport",
        lat=40.0801,
        lon=116.5846,
        timezone="Asia/Shanghai",
        source="awc",
        unit="C",
    ),
    "WMKK": Station(
        station_id="WMKK",
        city="Kuala Lumpur",
        name="Kuala Lumpur Intl Airport",
        lat=2.7456,
        lon=101.7099,
        timezone="Asia/Kuala_Lumpur",
        source="awc",
        unit="C",
    ),
    "RJTT": Station(
        station_id="RJTT",
        city="Tokyo",
        name="Tokyo Haneda Airport",
        lat=35.5533,
        lon=139.7811,
        timezone="Asia/Tokyo",
        source="awc",
        unit="C",
    ),
    "RKSI": Station(
        station_id="RKSI",
        city="Seoul",
        name="Seoul Incheon Intl Airport",
        lat=37.4692,
        lon=126.4505,
        timezone="Asia/Seoul",
        source="awc",
        unit="C",
    ),
    "NZWN": Station(
        station_id="NZWN",
        city="Wellington",
        name="Wellington Intl Airport",
        lat=-41.3272,
        lon=174.8053,
        timezone="Pacific/Auckland",
        source="awc",
        unit="C",
    ),
}

# Lowercase CITY alias -> station id. Used only for the question/rules
# cross-check ("does the city in the title contradict the rules station?"),
# never to pick a station on its own.
CITY_ALIASES: dict[str, str] = {
    "new york": "KLGA",
    "new york city": "KLGA",
    "nyc": "KLGA",
    "chicago": "KORD",
    "los angeles": "KLAX",
    "houston": "KHOU",
    "miami": "KMIA",
    "atlanta": "KATL",
    "dallas": "KDAL",
    "san francisco": "KSFO",
    "austin": "KAUS",
    "seattle": "KSEA",
    "denver": "KBKF",
    "buenos aires": "SAEZ",
    "sao paulo": "SBGR",
    "são paulo": "SBGR",
    "london": "EGLC",
    "paris": "LFPB",
    "munich": "EDDM",
    "ankara": "LTAC",
    "tel aviv": "LLBG",
    "beijing": "ZBAA",
    "kuala lumpur": "WMKK",
    "tokyo": "RJTT",
    "seoul": "RKSI",
    "wellington": "NZWN",
}

# Station-SPECIFIC names. These may identify the station from the resolution
# rules when no site URL / ICAO id is present, because they name the exact
# station, not just the city ("LaGuardia" can only be KLGA; "New York" cannot).
STATION_NAME_ALIASES: dict[str, str] = {
    "laguardia": "KLGA",
    "la guardia": "KLGA",
    "o'hare": "KORD",
    "ohare": "KORD",
    "los angeles international": "KLAX",
    "hobby": "KHOU",
    "miami international": "KMIA",
    "hartsfield": "KATL",
    "love field": "KDAL",
    "san francisco international": "KSFO",
    "bergstrom": "KAUS",
}


def get_station(station_id: str) -> Station | None:
    return STATIONS.get(station_id.upper())


def _match_alias(text: str, aliases: dict[str, str]) -> Station | None:
    lowered = text.lower()
    for alias in sorted(aliases, key=len, reverse=True):
        if alias in lowered:
            return STATIONS[aliases[alias]]
    return None


def station_for_city_alias(text: str) -> Station | None:
    """City-level match, for cross-checks only."""
    return _match_alias(text, CITY_ALIASES | STATION_NAME_ALIASES)


def station_for_station_name(text: str) -> Station | None:
    """Station-specific match, safe for identifying the resolution station."""
    return _match_alias(text, STATION_NAME_ALIASES)
