#!/usr/bin/env python3
"""Backtest / calibration report over stored history.

Replays the stored evaluations (forecast fair probabilities + executable
market prices captured each cycle) joined against realized outcomes, and
reports:

  * number of trades the strategy would have taken, win rate, ROI, max drawdown
  * Brier score of the model's fair probability vs the market's implied
    probability, side by side — THE number that matters. If the model is not
    better calibrated than the market, there is no edge.

Outcomes come from finalized observations applied to each stored claim, so
the report grows automatically as the bot runs and days resolve.

Usage: uv run python scripts/backtest.py [--db weatherbot.db] [--min-edge 0.08]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weatherbot import db  # noqa: E402
from weatherbot.markets.parser import WeatherClaim  # noqa: E402


def claim_outcome(claim: WeatherClaim, value: float) -> bool:
    if claim.comparator == "above":
        return value > claim.threshold_low
    if claim.comparator == "below":
        return value < claim.threshold_high
    return claim.threshold_low < value < claim.threshold_high


def brier(pairs: list[tuple[float, bool]]) -> float | None:
    if not pairs:
        return None
    return sum((p - (1.0 if won else 0.0)) ** 2 for p, won in pairs) / len(pairs)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="weatherbot.db")
    ap.add_argument("--min-edge", type=float, default=0.08)
    ap.add_argument("--min-confidence", type=float, default=0.6)
    args = ap.parse_args()

    conn = db.connect(args.db)
    rows = conn.execute(
        """SELECT e.*, o.high_f, o.low_f FROM evaluations e
           JOIN observations o
             ON o.station_id = json_extract(e.claim_json, '$.station_id')
            AND o.day = json_extract(e.claim_json, '$.resolution_date')
            AND o.final = 1
           WHERE e.claim_json IS NOT NULL AND e.fair_prob IS NOT NULL
             AND e.exec_price IS NOT NULL"""
    ).fetchall()

    if not rows:
        print("No resolved evaluation history yet. Let the bot run for a few days.")
        return 0

    model_pairs: list[tuple[float, bool]] = []
    market_pairs: list[tuple[float, bool]] = []
    trades: list[float] = []  # per-trade return on cost
    seen_markets: set[str] = set()

    for r in rows:
        claim = WeatherClaim.model_validate(json.loads(r["claim_json"]))
        value = r["high_f"] if claim.metric == "high_temp" else r["low_f"]
        if value is None:
            continue
        yes_won = claim_outcome(claim, value)
        model_pairs.append((r["fair_prob"], yes_won))
        market_pairs.append((r["market_price"], yes_won))

        # would-trade filter: one trade max per market, strategy thresholds
        if r["market_id"] in seen_markets:
            continue
        if (r["edge"] or 0) >= args.min_edge and (r["confidence"] or 0) >= args.min_confidence:
            seen_markets.add(r["market_id"])
            side_won = yes_won if r["side"] == "YES" else not yes_won
            price = r["exec_price"]
            ret = (1.0 - price) / price if side_won else -1.0
            trades.append(ret)

    print(f"resolved evaluation rows: {len(model_pairs)}")
    bm = brier(model_pairs)
    bk = brier(market_pairs)
    print("\n--- calibration (lower is better) ---")
    print(f"model Brier score : {bm:.4f}")
    print(f"market Brier score: {bk:.4f}")
    if bm is not None and bk is not None:
        verdict = "model BEATS market" if bm < bk else "model does NOT beat market — no edge"
        print(f"verdict           : {verdict}")

    print("\n--- simulated trades (1 unit per trade) ---")
    if not trades:
        print("no evaluations passed entry thresholds")
        return 0
    wins = sum(1 for t in trades if t > 0)
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for t in trades:
        equity += t
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    print(f"trades      : {len(trades)}")
    print(f"win rate    : {wins / len(trades):.1%}")
    print(f"total return: {equity:+.2f} units  (ROI {equity / len(trades):+.1%} per trade)")
    print(f"max drawdown: {max_dd:.2f} units")
    return 0


if __name__ == "__main__":
    sys.exit(main())
