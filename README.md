# weatherbot

Automated trading bot for Polymarket daily weather markets. It compares the
market's implied probability against a probability derived from ensemble
weather forecasts (GFS + ECMWF IFS via Open-Meteo) plus live NWS
observations, and takes a position only when the edge clears fees, slippage
and forecast error. Runs on a Mac mini under launchd, one cycle every
20 minutes, no human in the loop at execution time.

**It ships in paper mode.** Nothing in this repo can spend money without
you changing an env var AND passing an explicit CLI flag AND installing an
optional dependency. See "Going live" below.

## Layout

```
src/weatherbot/
  config.py        pydantic settings (config.yaml + env)
  db.py            SQLite schema/migrations/queries (stdlib sqlite3)
  logutil.py       logging with mandatory secret redaction
  markets/         Gamma discovery, CLOB orderbooks, resolution-rules parser
  forecast/        Open-Meteo ensembles, NWS observations, station allowlist,
                   calibrated probability distribution
  strategy/        executable-price edge, fractional-Kelly sizing, entry/exit rules
  execution/       paper and live executors + router
  risk.py          hard caps (code ceilings), kill switches, persistent halt
  heartbeat.py     healthchecks.io pings
  alerts.py        Telegram + email
  cycle.py         the 20-minute loop body (runs once, exits)
  cli.py           entrypoints
scripts/           backtest.py, review.py (nightly LLM review), check_no_secrets.py
ops/               macOS: launchd plists + install.sh + run_cycle.sh
                   Windows: install.ps1 + run_cycle.ps1 + run_review.ps1
prompts/           nightly review prompt
```

## Quick start (paper mode)

```bash
uv sync
uv run weatherbot init-db
uv run weatherbot cycle --dry-run     # full cycle vs live data, zero DB writes
uv run weatherbot cycle               # real paper cycle (writes to weatherbot.db)
uv run weatherbot status
uv run pytest
```

`--dry-run` prints a decision table with every market evaluated, the model
fair probability, the executable price for the intended size, the edge, and
the exact skip reason for every non-trade.

## Install on the Mac mini

```bash
./ops/install.sh
```

Creates `~/Library/Logs/weatherbot/`, copies `.env.example` to
`~/.weatherbot.env` (chmod 600), syncs the environment, loads two launchd
jobs (`com.rodrigo.weatherbot` every 1200 s with RunAtLoad, and the 6 am
nightly review), and prints the `pmset` / System Settings steps to do by
hand (disable sleep, auto-restart after power failure, auto-login).

Then:
1. Fill in `~/.weatherbot.env` and `config.yaml` (heartbeat URL, Telegram
   chat id, email addresses, NWS User-Agent contact).
2. Create a healthchecks.io check: period 20 min, grace 30 min. Two missed
   cycles ⇒ alert. Paste the ping URL into `config.yaml`.

## Install on Windows

Prereqs: Python 3.11+ and [uv](https://docs.astral.sh/uv/) on PATH (for the
nightly review, also the `claude` CLI).

```powershell
powershell -ExecutionPolicy Bypass -File ops\install.ps1
```

Creates `%LOCALAPPDATA%\weatherbot\logs`, copies `.env.example` to
`%USERPROFILE%\.weatherbot.env` and locks its NTFS ACL to your user (the
Windows equivalent of chmod 600 — `run_cycle.ps1` refuses to start if
anyone else is granted access), syncs the environment, and registers two
Task Scheduler jobs: `weatherbot-cycle` (at logon + every 20 minutes) and
`weatherbot-review` (daily 06:00). It then prints the manual steps: fill in
the key file/config, `powercfg` sleep settings (elevated), the BIOS
"Restore on AC Power Loss" option, auto-login or "run whether user is
logged on or not", and the healthchecks.io check.

Everything else — commands, DB, halt behavior, going live — is identical to
macOS; the wrappers only differ in how the schedule and key-file
protection are enforced. Timezone data comes from the `tzdata` package,
installed automatically on Windows.

## How a cycle works

1. If the persistent HALT flag is set: log and exit 0 (no heartbeat — you
   get paged until you clear it deliberately).
2. Heartbeat start ping.
3. Bankroll + open positions (paper: from SQLite; live: from CLOB), then
   reconcile against the local DB — discrepancies alert loudly.
4. Settle any paper positions whose resolution day completed.
5. Daily-loss (15%) and bankroll-floor ($25) kill switches — breach writes
   a `HALTED` row that survives restarts.
6. Discover weather markets via Gamma; parse each market's RESOLUTION RULES
   into a structured claim. Unknown station / ambiguous text ⇒ skip + log.
7. Ensemble forecasts (cached 30 min; stale > 4 h ⇒ unusable) + NWS hourly
   observations. Same-day markets with no observation data are skipped.
8. Fair probability + confidence per market; orderbook walk for the
   executable price; edge = fair − executable.
9. Entry gates (min edge 0.08, confidence 0.6, lead ≤ 3 d, slippage ≤ 0.02,
   volume ≥ $5k, no existing position, implausible-edge sanity gate),
   rank by edge × confidence, risk caps re-checked immediately before each
   order, fractional-Kelly sizing (0.25×), max 2 entries/cycle.
10. Route to paper or live executor; write everything to SQLite.
11. Heartbeat success ping. Any unhandled exception ⇒ no success ping,
    non-zero exit ⇒ healthchecks alerts you.

### Details that matter

- **Resolution source fidelity.** Polymarket temperature markets resolve
  off the weather.gov timeseries page's HOURLY rows in whole °F (NYC =
  LaGuardia `klga`, Chicago = O'Hare `kord` — not Central Park/Midway).
  The bot therefore uses only the hourly METAR readings (nearest :51),
  rounded to whole °F. 5-minute whole-°C readings are ignored — converting
  them adds up to ~0.9 °F of error, enough to falsely bust a 2° bucket.
- **Same-day markets** (the highest-confidence setup): the day's high is
  `max(observed_running_high, remaining_hours_max)`. The observed part is
  treated as certain; ensemble members provide the remaining-hours
  distribution, each member anchored by its error against the live
  reading. Once the observed value settles the market (running high
  already above the threshold), fair probability pins to 0.995/0.005.
- **Calibration**: (forecast, actual) pairs accumulate per station × lead
  bucket; bias correction and spread inflation activate only after 30
  pairs (identity before that).
- **Hard caps live in `risk.py` as code ceilings** — config can tighten
  them, never loosen: 5% position, 40% exposure, 8 positions, 15% per
  city, 2 entries/cycle, 15% daily loss, $25 floor.

## Halting

```bash
uv run weatherbot halt --reason "why"   # manual halt
uv run weatherbot clear-halt            # the ONLY way to clear any halt
```

Kill-switch halts (daily loss, bankroll floor) write the same persistent
row and also alert via Telegram + email. A halted bot skips every cycle
and does not ping the heartbeat, so healthchecks keeps reminding you.

## Going live (deliberately annoying)

Live order placement requires ALL of:

1. `uv sync --extra live` — installs `py-clob-client`, which a normal sync
   does not.
2. `TRADING_MODE=live` in `~/.weatherbot.env`.
3. The cycle invoked as `weatherbot cycle --i-understand-this-is-live`
   (edit `ops/run_cycle.sh` — or `ops/run_cycle.ps1` on Windows — to add
   the flag).
4. `POLYMARKET_PRIVATE_KEY` and `POLYMARKET_PROXY_ADDRESS` set in the key
   file (chmod 600 on macOS, owner-only NTFS ACL on Windows — the wrapper
   refuses to run otherwise).

Anything missing ⇒ the cycle refuses loudly and fails closed. The private
key is registered with the log redactor before any client is constructed
and never appears in logs, tracebacks, or the DB. A pre-commit hook
(`pre-commit install`) blocks committing anything that looks like key
material.

### Onboarding (no account yet)

1. Create a Polymarket account (email signup creates a proxy wallet;
   its address is shown in your Polymarket profile → this is
   `POLYMARKET_PROXY_ADDRESS`).
2. Export the private key: Polymarket → Settings → Export private key.
   Put it in `~/.weatherbot.env` as `POLYMARKET_PRIVATE_KEY`. If you
   instead sign in with your own wallet, change `signature_type` to 2 in
   `src/weatherbot/execution/live.py`.
3. Fund the account with USDC on Polygon.
4. Leave the bot in paper mode until the backtest Brier score beats the
   market's (below). Then follow "Going live".

## Backtest / validation

```bash
uv run python scripts/backtest.py
```

Replays stored evaluations against realized outcomes and reports trades,
win rate, ROI, max drawdown, and — the number that matters — the model's
Brier score vs the market's, side by side. If the model is not better
calibrated than the market, there is no edge; do not go live.

## Nightly review (the only LLM anywhere)

`scripts/review.py` (6 am launchd job) shells out to `claude -p` with
READ-ONLY tools (`Read`, `Bash(sqlite3:*)`) — no wallet access, no DB
writes, no `--dangerously-skip-permissions` — summarizes the last 24 h
(P&L, resolution surprises, calibration drift, suspicious skip-reason
patterns) and sends it to Telegram + email. The 20-minute trading loop is
deterministic Python; no LLM call exists anywhere in that path.

## Phase 2 (not enabled)

Taipei and Hong Kong markets resolve against non-NWS observation sources.
`stations.py` has a `source` field so adapters can be added; those cities
must not be added to the allowlist until an adapter + tests exist.
