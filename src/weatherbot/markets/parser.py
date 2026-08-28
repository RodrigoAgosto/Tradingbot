"""Parse a Polymarket market into a structured WeatherClaim.

Rules, in order of importance:

1. The claim is derived from the market's RESOLUTION RULES TEXT
   (description), not the title. The station must be identified there —
   either an explicit ICAO id (e.g. KNYC) or a station-specific alias
   ("Central Park") together with an explicit National Weather Service
   reference.
2. Any ambiguity means skip, with a machine-readable skip reason. A
   misparsed market is a guaranteed loss; a skipped market costs nothing.
3. Thresholds are normalized to CONTINUOUS OPEN-INTERVAL semantics so the
   probability model needs no comparator special cases:
       above:   event <=> value > threshold_low
       below:   event <=> value < threshold_high
       between: event <=> threshold_low < value < threshold_high
   Official station highs/lows are whole degrees F, so inclusive integer
   phrasing converts with +/-0.5:
       "84 or higher"     -> above,   threshold_low  = 83.5
       "above 84"         -> above,   threshold_low  = 84.5   (>84 means >=85)
       "between 78 and 79"-> between, 77.5 .. 79.5            (inclusive bucket)
       "84 or below"      -> below,   threshold_high = 84.5
       "below 84"         -> below,   threshold_high = 83.5
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel

from weatherbot.forecast.stations import (
    STATIONS,
    get_station,
    station_for_city_alias,
    station_for_station_name,
)


class WeatherClaim(BaseModel):
    market_id: str
    city: str
    station_id: str
    metric: Literal["high_temp", "low_temp", "precipitation", "snowfall"]
    comparator: Literal["above", "below", "between"]
    threshold_low: float | None
    threshold_high: float | None
    unit: Literal["F", "C", "in", "mm"]
    resolution_date: date
    resolution_source: str


class ParseResult(BaseModel):
    claim: WeatherClaim | None = None
    skip_reason: str | None = None


_MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"],
        start=1,
    )
}
_DATE_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(\d{4}))?",
    re.IGNORECASE,
)
_STATION_RE = re.compile(r"\b(K[A-Z]{3})\b")
# Polymarket rules typically name the station via a weather.gov timeseries URL,
# lowercase: ...weather.gov/wrh/timeseries?site=klga
_SITE_URL_RE = re.compile(r"[?&]site=([A-Za-z0-9]{3,5})\b")
_SOURCE_RE = re.compile(r"national weather service|weather\.gov|\bNOAA\b", re.IGNORECASE)
_NUM = r"(-?\d+(?:\.\d+)?)"

_BETWEEN_RE = re.compile(rf"between\s+{_NUM}\s*(?:°\s*[FC])?\s*and\s+{_NUM}", re.IGNORECASE)
_RANGE_RE = re.compile(rf"{_NUM}\s*[-–—]\s*{_NUM}\s*°?\s*[FC]?\b", re.IGNORECASE)
_OR_HIGHER_RE = re.compile(rf"{_NUM}\s*°?\s*[FC]?\s*(?:or|and)\s+(?:higher|above|greater|warmer|more)", re.IGNORECASE)
_OR_LOWER_RE = re.compile(rf"{_NUM}\s*°?\s*[FC]?\s*(?:or|and)\s+(?:lower|below|less|colder|fewer)", re.IGNORECASE)
_ABOVE_RE = re.compile(rf"(?:above|exceeds?|greater\s+than|higher\s+than|more\s+than|over)\s+{_NUM}", re.IGNORECASE)
_BELOW_RE = re.compile(rf"(?:below|under|less\s+than|lower\s+than|fewer\s+than)\s+{_NUM}", re.IGNORECASE)
# International phrasing: "be 30°C on August 28" = exactly 30 as displayed,
# i.e. the whole-degree bucket (29.5, 30.5). The trailing "on/in" guard keeps
# this from swallowing "be 30°C or higher".
_EXACT_RE = re.compile(rf"be\s+{_NUM}\s*°\s*[FC]\s+(?:on|in)\b", re.IGNORECASE)


def _detect_metric(text: str) -> str | None:
    t = text.lower()
    if re.search(r"\b(highest|high|max(?:imum)?)\s+temp", t):
        return "high_temp"
    if re.search(r"\b(lowest|low|min(?:imum)?)\s+temp", t):
        return "low_temp"
    if "snow" in t:
        return "snowfall"
    if re.search(r"\b(rain|precipitation|rainfall)\b", t):
        return "precipitation"
    if re.search(r"\btemperature\b", t):
        # temperature market without an explicit high/low qualifier is ambiguous
        return None
    return None


def _detect_unit(question: str, description: str, metric: str) -> str:
    """Every rules text carries the 'toggle between Fahrenheit and Celsius'
    display boilerplate, so keyword presence alone is useless. Precedence:
    the unit symbol in the QUESTION, then the 'degrees X' resolution phrase,
    then the 'displays °X' phrase."""
    q, d = question.lower(), description.lower()
    if metric in ("high_temp", "low_temp"):
        for text in (q,):
            if "°f" in text:
                return "F"
            if "°c" in text:
                return "C"
        if "degrees fahrenheit" in d:
            return "F"
        if "degrees celsius" in d:
            return "C"
        if "displays °f" in d:
            return "F"
        if "displays °c" in d:
            return "C"
        return "F"
    t = q + d
    if "mm" in t or "millimet" in t:
        return "mm"
    return "in"


def _is_integer(x: float) -> bool:
    return abs(x - round(x)) < 1e-9


def _detect_thresholds(text: str) -> tuple[str, float | None, float | None] | None:
    """Return (comparator, low, high) with open-interval normalization."""
    m = _BETWEEN_RE.search(text)
    if not m:
        m = _RANGE_RE.search(text)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        if b < a:
            a, b = b, a
        low = a - 0.5 if _is_integer(a) else a
        high = b + 0.5 if _is_integer(b) else b
        return "between", low, high

    m = _OR_HIGHER_RE.search(text)
    if m:
        v = float(m.group(1))
        return "above", (v - 0.5 if _is_integer(v) else v), None

    m = _OR_LOWER_RE.search(text)
    if m:
        v = float(m.group(1))
        return "below", None, (v + 0.5 if _is_integer(v) else v)

    m = _ABOVE_RE.search(text)
    if m:
        v = float(m.group(1))
        return "above", (v + 0.5 if _is_integer(v) else v), None

    m = _BELOW_RE.search(text)
    if m:
        v = float(m.group(1))
        return "below", None, (v - 0.5 if _is_integer(v) else v)

    m = _EXACT_RE.search(text)
    if m:
        v = float(m.group(1))
        return "between", v - 0.5, v + 0.5

    return None


def _detect_date(text: str, end_date: datetime | None) -> date | None:
    m = _DATE_RE.search(text)
    if m:
        month = _MONTHS[m.group(1).lower()]
        day = int(m.group(2))
        if m.group(3):
            return date(int(m.group(3)), month, day)
        # Infer year: the candidate nearest to the market's end date.
        base_year = end_date.year if end_date else date.today().year
        candidates = []
        for year in (base_year - 1, base_year, base_year + 1):
            try:
                candidates.append(date(year, month, day))
            except ValueError:
                continue
        if end_date:
            return min(candidates, key=lambda d: abs((d - end_date.date()).days))
        return candidates[0] if candidates else None
    if end_date:
        return end_date.date()
    return None


def _resolution_source(description: str) -> str:
    for sentence in re.split(r"(?<=[.!?])\s+", description):
        if re.search(r"national weather service|weather\.gov|resolve", sentence, re.IGNORECASE):
            return sentence.strip()[:300]
    return description.strip()[:300]


def parse_market(
    market_id: str,
    question: str,
    description: str,
    end_date: datetime | None,
) -> ParseResult:
    if not description or not description.strip():
        return ParseResult(skip_reason="no_resolution_text")

    full = f"{question}\n{description}"

    # --- station: must come from the resolution rules ---
    # Preferred: the resolution-source URL (weather.gov/...?site=klga).
    # Fallback: an explicit uppercase ICAO id in the text.
    station = None
    ids_in_rules = {s.upper() for s in _SITE_URL_RE.findall(description)}
    if not ids_in_rules:
        ids_in_rules = set(_STATION_RE.findall(description))
    known_ids = [s for s in ids_in_rules if s in STATIONS]
    unknown_ids = [s for s in ids_in_rules if s not in STATIONS]
    if len(known_ids) == 1:
        station = get_station(known_ids[0])
    elif len(known_ids) > 1:
        return ParseResult(skip_reason="multiple_stations_in_rules")
    elif unknown_ids:
        return ParseResult(skip_reason=f"station_not_in_allowlist:{sorted(unknown_ids)[0]}")
    else:
        # No explicit station id: allow a station-SPECIFIC name in the rules
        # ("LaGuardia"), but only alongside an explicit NWS/NOAA reference.
        # City-level names are never enough to pick a station.
        if _SOURCE_RE.search(description):
            station = station_for_station_name(description)
        if station is None:
            return ParseResult(skip_reason="no_station_in_resolution_rules")

    # Cross-check: a city mentioned in the question must not contradict the
    # station from the rules.
    q_station = station_for_city_alias(question)
    if q_station is not None and q_station.station_id != station.station_id:
        return ParseResult(skip_reason="question_city_contradicts_rules_station")

    metric = _detect_metric(full)
    if metric is None:
        return ParseResult(skip_reason="metric_unrecognized")

    thresholds = _detect_thresholds(question) or _detect_thresholds(description)
    if thresholds is None:
        return ParseResult(skip_reason="threshold_unrecognized")
    comparator, low, high = thresholds

    resolution_date = _detect_date(question, end_date) or _detect_date(description, end_date)
    if resolution_date is None:
        return ParseResult(skip_reason="resolution_date_unrecognized")

    unit = _detect_unit(question, description, metric)
    # The market's unit must match what the station's resolution page
    # displays — a mismatch means we misidentified something. Skip.
    if unit in ("F", "C") and unit != station.unit:
        return ParseResult(skip_reason=f"unit_mismatch:market_{unit}_station_{station.unit}")

    claim = WeatherClaim(
        market_id=market_id,
        city=station.city,
        station_id=station.station_id,
        metric=metric,  # type: ignore[arg-type]
        comparator=comparator,  # type: ignore[arg-type]
        threshold_low=low,
        threshold_high=high,
        unit=unit,  # type: ignore[arg-type]
        resolution_date=resolution_date,
        resolution_source=_resolution_source(description),
    )
    return ParseResult(claim=claim)
