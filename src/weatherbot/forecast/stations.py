"""Explicit allowlist of supported cities/stations.

A market that cannot be mapped to one of these stations is skipped —
unknown city means skip, always.

Phase 2 (NOT yet enabled): Taipei and Hong Kong. Those markets resolve
against non-NWS sources (CWA / HKO), so they need a new `source` adapter for
observations before they can be traded. The `source` field exists so they
slot in without restructuring; do not add them here until that adapter and
its tests exist.
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
    source: str  # observation source adapter: "nws" (only supported value today)


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
