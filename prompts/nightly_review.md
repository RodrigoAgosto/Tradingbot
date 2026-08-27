You are reviewing the last 24 hours of a Polymarket weather trading bot.
You have READ-ONLY access: the `Read` tool and `sqlite3` via Bash. The
database is `weatherbot.db` in the project directory. Do not modify anything.

Schema highlights:
- `cycles(id, started_at, finished_at, mode, status, bankroll, orders_placed, note)`
- `evaluations(cycle_id, market_id, question, claim_json, fair_prob, confidence, market_price, exec_price, side, edge, lead_days, decision, skip_reason, created_at)`
- `orders(market_id, side, action, price, shares, cost_usd, mode, status, created_at)`
- `positions(market_id, city, side, shares, avg_price, cost_usd, status, outcome, pnl_usd, resolution_date)`
- `observations(station_id, day, high_f, low_f, final)`
- `calibration_obs(station_id, metric, lead_bucket, forecast_mean, forecast_std, actual)`
- `halt(halted, reason, halted_at)`

Produce a review of the LAST 24 HOURS covering:

1. **P&L**: realized P&L from positions closed/resolved in the window, current
   bankroll, open exposure.
2. **Resolution surprises**: any resolved position where the recorded
   fair_prob was on the wrong side (fair_prob > 0.6 but lost, or < 0.4 but
   won). Quote market question, fair_prob, and the observed value.
3. **Calibration drift**: compare recent forecast_mean vs actual in
   calibration_obs per station. Flag any station/lead bucket whose mean error
   looks biased.
4. **Skip-reason patterns**: count skip_reasons across evaluations. Flag any
   reason that dominates in a way that suggests a parsing bug (especially
   threshold_unrecognized, no_station_in_resolution_rules,
   question_city_contradicts_rules_station) and quote one example question.
5. **Health**: cycles that did not finish with status ok, any halt row, gaps
   longer than 40 minutes between cycles.

End with a section exactly like this, under 900 characters, plain text (it is
sent as a Telegram message):

TELEGRAM SUMMARY:
<the summary>
