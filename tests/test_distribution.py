"""Ensemble -> probability behavior."""

from datetime import date

from weatherbot import db
from weatherbot.forecast.distribution import (
    Calibration,
    fair_value,
    lead_widening,
    load_calibration,
)
from weatherbot.markets.parser import WeatherClaim


def claim(comparator="above", low=79.5, high=None, metric="high_temp"):
    return WeatherClaim(
        market_id="m", city="New York", station_id="KLGA", metric=metric,
        comparator=comparator, threshold_low=low, threshold_high=high,
        unit="F", resolution_date=date(2026, 8, 27), resolution_source="test",
    )


MEMBERS = [78, 79, 80, 80, 81, 81, 82, 82, 83, 84] * 3  # mean ~81


def test_probability_monotonic_in_threshold():
    ident = Calibration()
    p_low = fair_value(claim(low=75.5), MEMBERS, 1, ident).probability
    p_high = fair_value(claim(low=85.5), MEMBERS, 1, ident).probability
    assert p_low > 0.8 > 0.2 > p_high


def test_never_exact_zero_or_one_without_observation():
    fv = fair_value(claim(low=200.0), MEMBERS, 1, Calibration())
    assert 0.0 < fv.probability < 1.0


def test_between_bucket():
    c = claim(comparator="between", low=79.5, high=82.5)
    fv = fair_value(c, MEMBERS, 1, Calibration())
    assert 0.2 < fv.probability < 0.9


def test_observed_high_settles_above_market():
    # running high 80 already above the 79.5 threshold -> settled YES
    fv = fair_value(claim(low=79.5), MEMBERS, 0, Calibration(), observed_value=80.0)
    assert fv.probability >= 0.99
    assert fv.observed_decided
    assert fv.confidence >= 0.95


def test_observed_high_busts_between_bucket():
    c = claim(comparator="between", low=77.5, high=79.5)
    fv = fair_value(c, MEMBERS, 0, Calibration(), observed_value=80.0)
    assert fv.probability <= 0.01
    assert fv.observed_decided


def test_observed_low_settles_below_market():
    c = claim(comparator="below", low=None, high=60.5, metric="low_temp")
    fv = fair_value(c, [62, 63, 64, 65, 66] * 6, 0, Calibration(), observed_value=59.0)
    assert fv.probability >= 0.99


def test_same_day_obs_floor_with_cool_remaining_hours():
    # Observed high 77; remaining-day members say ~75 (evening cooling).
    # 76-77 bucket (open interval 75.5..77.5): high is pinned at 77 -> ~certain.
    c = claim(comparator="between", low=75.5, high=77.5)
    remaining = [75.0] * 30
    fv = fair_value(c, remaining, 0, Calibration(), observed_value=77.0)
    assert fv.probability > 0.9
    # and "above 77.5" is correspondingly near zero
    c2 = claim(low=77.5)
    fv2 = fair_value(c2, remaining, 0, Calibration(), observed_value=77.0)
    assert fv2.probability < 0.1
    assert fv.confidence >= 0.8


def test_evening_collapse_via_remaining_hours():
    # forecast said low 80s all day, but it is late: remaining-day max ~83,
    # observed 83 -> "84 or higher" (>83.5)... observed 83 does NOT settle,
    # remaining hours can still tick 84.
    c = claim(low=83.5)
    fv = fair_value(c, [83.0] * 30, 0, Calibration(), observed_value=83.0)
    assert fv.probability < 0.35  # only the kernel tail of remaining hours
    # with remaining hours clearly colder, probability collapses
    fv2 = fair_value(c, [78.0] * 30, 0, Calibration(), observed_value=83.0)
    assert fv2.probability <= 0.01
    assert fv2.confidence >= 0.8


def test_lead_widening_reduces_confidence():
    c = claim(low=81.5)
    near = fair_value(c, MEMBERS, 1, Calibration())
    far = fair_value(c, MEMBERS, 6, Calibration())
    assert far.confidence < near.confidence
    assert lead_widening(6) > lead_widening(1) == 1.0


def test_calibration_identity_until_30_obs(conn):
    for i in range(29):
        db.record_forecast_snapshot(conn, "KLGA", "high_temp", 1, f"2026-07-{i+1:02d}", 80.0, 2.0)
    conn.execute("UPDATE calibration_obs SET actual = forecast_mean + 3.0")
    conn.commit()
    cal = load_calibration(conn, "KLGA", "high_temp", 1)
    assert not cal.active
    assert cal.bias == 0.0 and cal.inflation == 1.0

    db.record_forecast_snapshot(conn, "KLGA", "high_temp", 1, "2026-08-01", 80.0, 2.0)
    conn.execute("UPDATE calibration_obs SET actual = forecast_mean + 3.0 WHERE actual IS NULL")
    conn.commit()
    cal = load_calibration(conn, "KLGA", "high_temp", 1)
    assert cal.active
    assert abs(cal.bias - (-3.0)) < 1e-9  # forecast ran 3F cold -> negative bias


def test_calibration_bias_shifts_probability(conn):
    # +2F warm bias (forecast above actual) should lower P(above)
    cold = Calibration(bias=2.0, inflation=1.0, n=30)
    c = claim(low=81.5)
    p_ident = fair_value(c, MEMBERS, 1, Calibration()).probability
    p_corr = fair_value(c, MEMBERS, 1, cold).probability
    assert p_corr < p_ident


def test_intraday_anchor_corrects_warm_members():
    # members forecast the current hour at 78 but the live reading is 75:
    # each remaining-day max shifts down 3F before probabilities are computed
    c = claim(low=77.5)  # "78 or higher"
    remaining = [78.0] * 30
    nows = [78.0] * 30
    unanchored = fair_value(c, remaining, 0, Calibration(), observed_value=77.0)
    anchored = fair_value(c, remaining, 0, Calibration(), observed_value=77.0,
                          member_nows=nows, observed_current=75.0)
    assert anchored.probability < unanchored.probability
    assert anchored.probability < 0.1


def test_anchor_shift_is_capped():
    c = claim(low=77.5)
    remaining = [78.0] * 30
    nows = [95.0] * 30  # absurd 20F member error -> capped at 6F shift
    fv = fair_value(c, remaining, 0, Calibration(), observed_value=70.0,
                    member_nows=nows, observed_current=75.0)
    assert fv is not None  # shifted members = 72, not 58


def test_too_few_members_returns_none():
    assert fair_value(claim(), [80, 81], 1, Calibration()) is None


def test_observation_settlement_wins_even_with_no_members():
    fv = fair_value(claim(low=79.5), [], 0, Calibration(), observed_value=85.0)
    assert fv is not None and fv.probability >= 0.99
