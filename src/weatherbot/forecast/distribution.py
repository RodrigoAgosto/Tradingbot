"""Ensemble members -> calibrated probability distribution -> fair probability.

The pipeline for one market:

    members (per-member daily extreme, deg F; for SAME-DAY markets these are
    the REMAINING-day extremes, hours >= now, from openmeteo._reduce_daily)
      -> calibration adjustment (per station x lead bucket: bias + spread
         inflation; identity until >= 30 (forecast, actual) pairs exist)
      -> lead-time spread widening (never confident far out)
      -> same-day composition with the observed running extreme:
         the day's high is max(observed_so_far, remaining_max), where
         observed_so_far is (nearly) certain. Probabilities are computed
         analytically from that composition, so no artificial forecast
         uncertainty is smeared over the already-observed part of the day.
      -> smoothed empirical probability (gaussian kernel per member, so a
         small ensemble never emits a hard 0 or 1)

Thresholds use the parser's open-interval semantics:
    above:   value > threshold_low
    below:   value < threshold_high
    between: threshold_low < value < threshold_high

Observed values arrive as whole degrees F (matching the resolution page's
precision), and thresholds sit on half degrees, so observation comparisons
are never ambiguous.
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass

from weatherbot import db
from weatherbot.markets.parser import WeatherClaim

log = logging.getLogger(__name__)

MIN_CALIBRATION_OBS = 30
MIN_MEMBERS = 5

P_FLOOR = 0.005
P_CEIL = 0.995


@dataclass
class Calibration:
    bias: float = 0.0        # mean(forecast_mean - actual); subtracted from members
    inflation: float = 1.0   # multiplier on ensemble spread
    n: int = 0

    @property
    def active(self) -> bool:
        return self.n >= MIN_CALIBRATION_OBS


@dataclass
class FairValue:
    probability: float
    confidence: float
    forecast_mean: float | None
    forecast_std: float | None
    n_members: int
    observed_decided: bool = False


def load_calibration(conn, station_id: str, metric: str, lead_bucket: int) -> Calibration:
    """Identity until MIN_CALIBRATION_OBS pairs exist for this bucket."""
    pairs = db.get_calibration_pairs(conn, station_id, metric, lead_bucket)
    if len(pairs) < MIN_CALIBRATION_OBS:
        return Calibration(n=len(pairs))
    errors = [fmean - actual for fmean, _, actual in pairs]
    bias = statistics.fmean(errors)
    mean_spread = statistics.fmean(fstd for _, fstd, _ in pairs)
    error_spread = statistics.pstdev(errors)
    if mean_spread <= 1e-6:
        inflation = 1.0
    else:
        inflation = min(3.0, max(0.75, error_spread / mean_spread))
    return Calibration(bias=bias, inflation=inflation, n=len(pairs))


def lead_widening(lead_days: float) -> float:
    """Extra spread inflation with lead time: identity same-day, +10%/day after."""
    return 1.0 + 0.10 * max(0.0, lead_days - 1.0)


def _norm_cdf(x: float, mean: float, sigma: float) -> float:
    return 0.5 * (1.0 + math.erf((x - mean) / (sigma * math.sqrt(2.0))))


def _mix_cdf(x: float, members: list[float], sigma: float) -> float:
    """P(member value <= x) under the gaussian-kernel mixture."""
    return statistics.fmean(_norm_cdf(x, m, sigma) for m in members)


def _observed_decides(claim: WeatherClaim, observed_value: float) -> bool | None:
    """If the running observation already settles the market, return the
    outcome (True/False). None when still open.

    For high_temp the running max is a floor: it can only rise. So the event
    is settled once the floor crosses a threshold. Mirror logic for low_temp.
    """
    if claim.metric == "high_temp":
        if claim.comparator == "above":
            return True if observed_value > claim.threshold_low else None
        if claim.comparator == "below":
            return False if observed_value >= claim.threshold_high else None
        # between: bucket busted once high exceeds the upper bound
        return False if observed_value >= claim.threshold_high else None
    if claim.metric == "low_temp":
        if claim.comparator == "below":
            return True if observed_value < claim.threshold_high else None
        if claim.comparator == "above":
            return False if observed_value <= claim.threshold_low else None
        return False if observed_value <= claim.threshold_low else None
    return None


def _same_day_high_prob(claim: WeatherClaim, obs: float,
                        members: list[float], sigma: float) -> float:
    """P(event) where day_high = max(obs, remaining_max). obs is certain;
    G(x) = P(remaining_max <= x) comes from the kernel mixture."""
    def G(x: float) -> float:
        return _mix_cdf(x, members, sigma)

    if claim.comparator == "above":
        t = claim.threshold_low
        return 1.0 if obs > t else 1.0 - G(t)
    if claim.comparator == "below":
        t = claim.threshold_high
        return 0.0 if obs >= t else G(t)
    a, b = claim.threshold_low, claim.threshold_high
    if obs >= b:
        return 0.0
    if obs > a:
        return G(b)
    return G(b) - G(a)


def _same_day_low_prob(claim: WeatherClaim, obs: float,
                       members: list[float], sigma: float) -> float:
    """P(event) where day_low = min(obs, remaining_min)."""
    def F(x: float) -> float:
        return _mix_cdf(x, members, sigma)

    if claim.comparator == "below":
        t = claim.threshold_high
        return 1.0 if obs < t else F(t)
    if claim.comparator == "above":
        t = claim.threshold_low
        return 0.0 if obs <= t else 1.0 - F(t)
    a, b = claim.threshold_low, claim.threshold_high
    if obs <= a:
        return 0.0
    if obs < b:
        return 1.0 - F(a)
    return F(b) - F(a)


def _event_prob(members: list[float], sigma: float, claim: WeatherClaim) -> float:
    if claim.comparator == "above":
        return 1.0 - _mix_cdf(claim.threshold_low, members, sigma)
    if claim.comparator == "below":
        return _mix_cdf(claim.threshold_high, members, sigma)
    return (_mix_cdf(claim.threshold_high, members, sigma)
            - _mix_cdf(claim.threshold_low, members, sigma))


MAX_ANCHOR_SHIFT = 6.0  # cap the per-member intraday correction, deg F


def fair_value(
    claim: WeatherClaim,
    members: list[float],
    lead_days: float,
    calibration: Calibration,
    observed_value: float | None = None,
    member_nows: list[float] | None = None,
    observed_current: float | None = None,
) -> FairValue | None:
    """Fair probability + confidence for a temperature claim.

    For same-day markets (lead_days == 0), `members` must be the
    REMAINING-day extremes and `observed_value` the running observed
    high (high_temp) or low (low_temp) in whole degrees F.

    member_nows/observed_current enable the intraday anchor: each member's
    remaining-day extreme is shifted by (observed_current - member_now),
    so members that are running warm/cold against the live reading are
    corrected before probabilities are computed.
    """
    if claim.metric not in ("high_temp", "low_temp"):
        return None

    # All tuned magnitudes below (sigma floor, agreement scale, anchor cap)
    # are expressed in deg F; deg C values are 5/9 the size.
    uscale = 1.0 if claim.unit == "F" else 5.0 / 9.0

    same_day = lead_days <= 0 and observed_value is not None

    # 1. hard settlement by observation — checked before anything else
    if observed_value is not None:
        settled = _observed_decides(claim, observed_value)
        if settled is not None:
            p = P_CEIL if settled else P_FLOOR
            mean = statistics.fmean(members) if members else None
            std = statistics.pstdev(members) if len(members) > 1 else 0.0
            return FairValue(p, 0.97, mean, std if members else None,
                             len(members), observed_decided=True)

    if len(members) < MIN_MEMBERS:
        return None

    # intraday anchor: correct each member by its error vs the live reading
    anchored = False
    if (
        same_day
        and observed_current is not None
        and member_nows is not None
        and len(member_nows) == len(members)
    ):
        cap = MAX_ANCHOR_SHIFT * uscale
        shifts = [
            max(-cap, min(cap, observed_current - now))
            for now in member_nows
        ]
        members = [m + s for m, s in zip(members, shifts)]
        anchored = True

    raw_mean = statistics.fmean(members)
    raw_std = statistics.pstdev(members)

    # 2. calibration + lead widening
    inflation = calibration.inflation * lead_widening(lead_days)
    center = raw_mean - calibration.bias
    adjusted = [center + (m - raw_mean) * inflation for m in members]
    adj_std = statistics.pstdev(adjusted)
    sigma = max(0.6 * uscale, 0.25 * adj_std)

    # 3. event probability
    if same_day:
        if claim.metric == "high_temp":
            p = _same_day_high_prob(claim, observed_value, adjusted, sigma)
        else:
            p = _same_day_low_prob(claim, observed_value, adjusted, sigma)
    else:
        p = _event_prob(adjusted, sigma, claim)
    p = min(P_CEIL, max(P_FLOOR, p))

    # 4. confidence: ensemble agreement x lead time x sample size
    agreement = 1.0 / (1.0 + adj_std / (4.0 * uscale))
    lead_factor = min(1.0, max(0.2, 1.0 - 0.12 * max(0.0, lead_days - 0.5)))
    size_factor = min(1.0, len(members) / 30.0)
    confidence = agreement * lead_factor * size_factor
    if same_day and anchored:
        # observed floor/cap plus live-anchored remaining hours: the model
        # is working from mostly-known information
        confidence = max(confidence, 0.8)
    confidence = min(1.0, max(0.0, confidence))

    return FairValue(p, confidence, raw_mean, raw_std, len(members))


def lead_bucket(lead_days: float) -> int:
    return max(0, int(lead_days))
