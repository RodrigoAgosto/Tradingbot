You are reviewing the last 24 hours of a Polymarket weather trading bot.

All the data you need has already been extracted, read-only, into this file:

    __SNAPSHOT_PATH__

Read that file with the Read tool. It is your ONLY data source — you have no
database or shell access, and you need none. If a section of the snapshot is
missing or empty, say so for that section instead of guessing. Never invent
numbers.

Produce a review covering:

1. **P&L**: realized P&L from the positions closed/resolved in the window,
   current cash bankroll, and open exposure (sum of open position cost).
2. **Resolution surprises**: any resolved position whose entry-time
   fair_prob was on the wrong side (model said its side had > 0.6
   probability but it lost, or < 0.4 and it won). Quote the market
   question, the fair_prob, and the outcome.
3. **Calibration drift**: per-station mean_error in the calibration section
   is (forecast - actual); flag any station/lead bucket whose error looks
   systematically biased, especially with n_resolved >= 5.
4. **Skip-reason patterns**: flag any reason whose count suggests a parsing
   bug (especially threshold_unrecognized, no_station_in_resolution_rules
   on non-Hong-Kong/Taipei markets, unit_mismatch, or
   question_city_contradicts_rules_station) and quote the example question.
   no_station_in_resolution_rules on Hong Kong/Taipei markets is expected
   and fine.
5. **Health**: cycles not finishing ok, any halt, notes that look like
   errors or reconciliation discrepancies.

End with a section exactly like this, under 900 characters, plain text (it
is sent as a Telegram message):

TELEGRAM SUMMARY:
<the summary>
