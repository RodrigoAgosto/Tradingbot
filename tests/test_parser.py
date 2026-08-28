"""Market question/rules -> WeatherClaim parsing.

The rules text below mirrors the real Polymarket weather boilerplate,
including the Celsius display-toggle sentence that must NOT make the market
look like a Celsius market, and the lowercase station id in the
weather.gov timeseries URL.
"""

from datetime import date, datetime, timezone

from weatherbot.markets.parser import parse_market

END = datetime(2026, 8, 28, 12, tzinfo=timezone.utc)

NYC_RULES = (
    "This market will resolve to the temperature range that contains the highest "
    "temperature recorded by NOAA at the LaGuardia Airport Station in degrees "
    "Fahrenheit on 27 Aug '26.\n\n"
    "The resolution source for this market will be information from NOAA, "
    'specifically the highest reading under the "Temp" column for all times on '
    "this day, available here: https://www.weather.gov/wrh/timeseries?site=klga\n\n"
    'To toggle between Fahrenheit and Celsius, click the "Switch to US Units" '
    "button until the relevant table displays °F."
)

CHI_RULES = NYC_RULES.replace("LaGuardia Airport", "O'Hare Airport").replace(
    "site=klga", "site=kord"
)


def test_parse_bucket_market():
    r = parse_market(
        "m1",
        "Will the highest temperature in New York City be between 76-77°F on August 27?",
        NYC_RULES, END,
    )
    c = r.claim
    assert c is not None, r.skip_reason
    assert c.station_id == "KLGA"
    assert c.city == "New York"
    assert c.metric == "high_temp"
    assert c.comparator == "between"
    assert c.threshold_low == 75.5
    assert c.threshold_high == 77.5
    assert c.resolution_date == date(2026, 8, 27)
    assert c.unit == "F"


def test_parse_or_higher():
    r = parse_market(
        "m2",
        "Will the highest temperature in New York City be 84°F or higher on August 27?",
        NYC_RULES, END,
    )
    c = r.claim
    assert c is not None, r.skip_reason
    assert c.comparator == "above"
    assert c.threshold_low == 83.5  # inclusive 84 -> open-interval 83.5


def test_parse_or_below():
    r = parse_market(
        "m3",
        "Will the highest temperature in New York City be 70°F or below on August 27?",
        NYC_RULES, END,
    )
    c = r.claim
    assert c is not None, r.skip_reason
    assert c.comparator == "below"
    assert c.threshold_high == 70.5


def test_parse_chicago_low_below():
    r = parse_market(
        "m4",
        "Will the lowest temperature in Chicago be below 60°F on August 27?",
        CHI_RULES.replace("highest", "lowest"), END,
    )
    c = r.claim
    assert c is not None, r.skip_reason
    assert c.station_id == "KORD"
    assert c.metric == "low_temp"
    assert c.comparator == "below"
    assert c.threshold_high == 59.5  # strict below 60 on integer obs


def test_station_name_alias_without_url():
    rules = ("Resolves to the highest temperature recorded by NOAA at the "
             "LaGuardia Airport Station in degrees Fahrenheit on August 27, 2026.")
    r = parse_market("m5", "Will the highest temperature in NYC be 84°F or higher on August 27?",
                     rules, END)
    assert r.claim is not None and r.claim.station_id == "KLGA"


def test_city_alias_alone_is_not_enough():
    rules = ("Resolves to the highest temperature recorded by NOAA at the JFK Airport "
             "Station in New York in degrees Fahrenheit on August 27, 2026.")
    r = parse_market("m6", "Will the highest temperature in NYC be 84°F or higher on August 27?",
                     rules, END)
    assert r.claim is None
    assert r.skip_reason == "no_station_in_resolution_rules"


def test_unknown_station_skipped():
    rules = NYC_RULES.replace("site=klga", "site=kphx").replace("LaGuardia", "Phoenix Sky Harbor")
    r = parse_market("m7", "Will the highest temperature in Phoenix be 105°F or higher on August 27?",
                     rules, END)
    assert r.claim is None
    assert "station_not_in_allowlist:KPHX" in r.skip_reason


def test_no_resolution_text_skipped():
    r = parse_market("m8", "Will the highest temperature in NYC be 84°F or higher?", "", END)
    assert r.claim is None
    assert r.skip_reason == "no_resolution_text"


def test_city_station_mismatch_skipped():
    r = parse_market(
        "m9",
        "Will the highest temperature in Chicago be 84°F or higher on August 27?",
        NYC_RULES, END,
    )
    assert r.claim is None
    assert r.skip_reason == "question_city_contradicts_rules_station"


def test_celsius_market_on_fahrenheit_station_skipped():
    # a deg C market claiming to resolve at LaGuardia (a deg F station)
    # means we misread something -> skip
    r = parse_market("m10", "Will the highest temperature in New York City be 29°C or higher "
                     "on August 27?", NYC_RULES, END)
    assert r.claim is None
    assert "unit_mismatch" in r.skip_reason


BEIJING_RULES = (
    "This market will resolve to the temperature that is the highest temperature "
    "recorded by NOAA at the Beijing Capital International Airport Station in degrees "
    "Celsius on 28 Aug '26.\n\n"
    "The resolution source for this market will be information from NOAA, available "
    "here: https://www.weather.gov/wrh/timeseries?site=zbaa\n\n"
    'To toggle between Fahrenheit and Celsius, click the button until the relevant '
    "table displays °C."
)


def test_parse_celsius_exact_bucket():
    r = parse_market("m11", "Will the highest temperature in Beijing be 24°C on August 28?",
                     BEIJING_RULES, END)
    c = r.claim
    assert c is not None, r.skip_reason
    assert c.station_id == "ZBAA"
    assert c.unit == "C"
    assert c.comparator == "between"
    assert c.threshold_low == 23.5
    assert c.threshold_high == 24.5


def test_parse_celsius_or_higher():
    r = parse_market("m12", "Will the highest temperature in Beijing be 30°C or higher on August 28?",
                     BEIJING_RULES, END)
    c = r.claim
    assert c is not None, r.skip_reason
    assert c.comparator == "above"
    assert c.threshold_low == 29.5
    assert c.unit == "C"


def test_no_threshold_skipped():
    r = parse_market("m13", "Will it be hot in New York City on August 27?", NYC_RULES.split("between")[0] +
                     " https://www.weather.gov/wrh/timeseries?site=klga", END)
    assert r.claim is None
    assert r.skip_reason == "threshold_unrecognized"
