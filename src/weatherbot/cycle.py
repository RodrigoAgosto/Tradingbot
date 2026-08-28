"""One iteration of the 20-minute loop. Runs once and exits; launchd owns
the schedule. Deterministic Python only — no LLM anywhere in this path.

Fail-closed rules:
  * any failure to fetch markets, forecasts or books => no NEW positions
    from the affected data this cycle (skip with a logged reason);
  * forecast data older than 4 h or market data older than 60 s is stale
    and treated as missing;
  * dry-run mode runs the entire pipeline against a throwaway in-memory
    copy of the DB, so the real DB sees zero writes.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import httpx

from weatherbot import db, heartbeat
from weatherbot.alerts import notify_urgent
from weatherbot.config import Settings
from weatherbot.execution.router import get_executor
from weatherbot.execution.types import CloseIntent, OrderIntent
from weatherbot.forecast import awc, distribution, nws, openmeteo
from weatherbot.forecast.stations import STATIONS, get_station
from weatherbot.markets import clob, gamma
from weatherbot.markets.parser import WeatherClaim, parse_market
from weatherbot.risk import RiskManager
from weatherbot.strategy import rules, sizing
from weatherbot.strategy.edge import compute_edge

log = logging.getLogger(__name__)


@dataclass
class Decision:
    market_id: str
    question: str
    decision: str            # enter|exit|hold|skip
    skip_reason: str | None = None
    claim: WeatherClaim | None = None
    fair_prob: float | None = None
    confidence: float | None = None
    market_price: float | None = None
    exec_price: float | None = None
    side: str | None = None
    edge: float | None = None
    lead_days: float | None = None
    volume_24h: float | None = None
    cost_usd: float | None = None


@dataclass
class CycleReport:
    status: str = "ok"
    bankroll: float = 0.0
    orders_placed: int = 0
    decisions: list[Decision] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _dry_run_conn(db_path: str) -> sqlite3.Connection:
    """In-memory copy of the DB: identical code path, zero real writes."""
    src = db.connect(db_path)
    mem = sqlite3.connect(":memory:")
    src.backup(mem)
    src.close()
    mem.row_factory = sqlite3.Row
    mem.execute("PRAGMA foreign_keys=ON")
    return mem


def _record(conn, cycle_id: int, d: Decision) -> None:
    db.record_evaluation(
        conn,
        cycle_id,
        {
            "market_id": d.market_id,
            "question": d.question,
            "claim": d.claim.model_dump(mode="json") if d.claim else None,
            "fair_prob": d.fair_prob,
            "confidence": d.confidence,
            "market_price": d.market_price,
            "exec_price": d.exec_price,
            "side": d.side,
            "edge": d.edge,
            "lead_days": d.lead_days,
            "volume_24h": d.volume_24h,
            "decision": d.decision,
            "skip_reason": d.skip_reason,
        },
    )


def _fetch_obs(client, station, day, user_agent) -> nws.ObservedDay | None:
    """Observation fetch dispatched by the station's source adapter."""
    if station.source == "awc":
        return awc.fetch_day_observations(client, station, day, user_agent)
    return nws.fetch_day_observations(client, station, day, user_agent)


def _update_observations(conn, client, station, cfg, today_local: date) -> nws.ObservedDay | None:
    """Fetch today's running obs; also finalize yesterday for calibration."""
    yesterday = today_local - timedelta(days=1)
    row = db.get_observation(conn, station.station_id, yesterday.isoformat())
    if row is None or not row["final"]:
        obs_y = _fetch_obs(client, station, yesterday, cfg.forecast.nws_user_agent)
        if obs_y is not None and obs_y.high_f is not None:
            db.upsert_observation(
                conn, station.station_id, yesterday.isoformat(),
                obs_y.high_f, obs_y.low_f,
                obs_y.last_obs_at.isoformat() if obs_y.last_obs_at else None,
                final=obs_y.complete,
            )

    obs = _fetch_obs(client, station, today_local, cfg.forecast.nws_user_agent)
    if obs is not None and obs.high_f is not None:
        db.upsert_observation(
            conn, station.station_id, today_local.isoformat(),
            obs.high_f, obs.low_f,
            obs.last_obs_at.isoformat() if obs.last_obs_at else None,
            final=False,
        )
    return obs


def _settle_paper_positions(conn, client, executor, cfg: Settings, report: CycleReport) -> None:
    """Resolve paper positions whose resolution day has fully passed."""
    for pos in db.get_open_positions(conn):
        if not pos["resolution_date"] or not pos["claim_json"]:
            continue
        claim = WeatherClaim.model_validate(json.loads(pos["claim_json"]))
        station = get_station(claim.station_id)
        if station is None:
            continue
        today_local = nws.local_now(station).date()
        if claim.resolution_date >= today_local:
            continue
        row = db.get_observation(conn, claim.station_id, claim.resolution_date.isoformat())
        if row is None or not row["final"]:
            obs = _fetch_obs(client, station, claim.resolution_date,
                             cfg.forecast.nws_user_agent)
            if obs is None or obs.high_f is None:
                report.notes.append(f"settlement pending (no obs): {pos['market_id']}")
                continue
            db.upsert_observation(conn, claim.station_id, claim.resolution_date.isoformat(),
                                  obs.high_f, obs.low_f,
                                  obs.last_obs_at.isoformat() if obs.last_obs_at else None,
                                  final=obs.complete)
            value = obs.high_f if claim.metric == "high_temp" else obs.low_f
            if not obs.complete:
                continue
        else:
            value = row["high_f"] if claim.metric == "high_temp" else row["low_f"]
        if value is None:
            continue
        yes_outcome = _claim_outcome(claim, value)
        won = yes_outcome if pos["side"] == "YES" else not yes_outcome
        pnl = executor.settle(pos["market_id"], won, pos["shares"], pos["cost_usd"])
        report.notes.append(
            f"settled {pos['market_id']} {pos['side']} {'WON' if won else 'LOST'} "
            f"(obs={value:.0f}) pnl=${pnl:.2f}"
        )


def _claim_outcome(claim: WeatherClaim, value: float) -> bool:
    if claim.comparator == "above":
        return value > claim.threshold_low
    if claim.comparator == "below":
        return value < claim.threshold_high
    return claim.threshold_low < value < claim.threshold_high


def run_cycle(settings: Settings, dry_run: bool = False, live_ack: bool = False) -> CycleReport:
    now = datetime.now(timezone.utc)
    report = CycleReport()

    conn = _dry_run_conn(settings.db_path) if dry_run else db.connect(settings.db_path)

    # 1. halt flag — checked before anything else, including heartbeats
    halted, reason = db.is_halted(conn)
    if halted:
        log.warning("HALTED (%s) — no trading. Clear with `weatherbot clear-halt`.", reason)
        report.status = "halted"
        conn.close()
        return report

    # 2. heartbeat start
    if not dry_run:
        heartbeat.ping_start(settings.heartbeat)

    cycle_id = db.start_cycle(conn, settings.mode)
    client = httpx.Client(follow_redirects=True)
    risk = RiskManager(conn, settings.risk)
    orders_placed = 0

    try:
        # 3. bankroll + open positions
        executor = get_executor(conn, settings, live_ack)
        bankroll = executor.bankroll()
        report.bankroll = bankroll

        # 4. reconcile (live only; in paper the DB is the source of truth)
        if executor.mode == "live":
            _reconcile_live(conn, executor, settings, report)

        # 4b. settle finished paper positions so P&L is realized before the
        #     daily-loss check
        if executor.mode == "paper":
            _settle_paper_positions(conn, client, executor, settings, report)
            bankroll = executor.bankroll()
            report.bankroll = bankroll

        # 5. daily loss / floor kill switches — measured on EQUITY (cash +
        #    open position cost basis). Cash alone would read every freshly
        #    opened position as a "loss" and false-trigger the halt.
        equity = risk.equity(bankroll)
        day_start = db.get_day_start_bankroll(conn, now.date(), equity)
        halt_reason = risk.check_kill_switches(equity, day_start)
        if halt_reason:
            if not dry_run:
                notify_urgent(settings.alerts, f"TRADING HALTED: {halt_reason}")
            db.finish_cycle(conn, cycle_id, "halted", bankroll,
                            len(db.get_open_positions(conn)), 0, halt_reason)
            report.status = "halted"
            if not dry_run:
                heartbeat.ping_success(settings.heartbeat)
            return report

        # 6. discover markets (failure => fail closed: exits still run on
        #    cached claims? No — without fresh market data we do nothing new)
        try:
            markets = gamma.fetch_active_weather_markets(client)
        except Exception as exc:
            log.error("gamma discovery failed, no new positions this cycle: %s", exc)
            report.status = "degraded"
            report.notes.append(f"gamma_failed:{exc}")
            markets = []

        # 7. parse claims; trade only cities enabled in config
        parsed: list[tuple[gamma.GammaMarket, WeatherClaim]] = []
        for market in markets:
            result = parse_market(market.id, market.question, market.description, market.end_date)
            if result.claim is None:
                d = Decision(market.id, market.question, "skip", result.skip_reason)
                report.decisions.append(d)
                _record(conn, cycle_id, d)
                continue
            if result.claim.city not in settings.cities:
                d = Decision(market.id, market.question, "skip",
                             f"city_not_enabled:{result.claim.city}", claim=result.claim)
                report.decisions.append(d)
                _record(conn, cycle_id, d)
                continue
            parsed.append((market, result.claim))

        # 8-9. forecasts + observations per station (batched, cached).
        # Open positions' stations are included so exits can be evaluated.
        stations_needed = {c.station_id for _, c in parsed}
        for pos in db.get_open_positions(conn):
            if pos["station_id"] in STATIONS:
                stations_needed.add(pos["station_id"])
        ensembles: dict[str, openmeteo.EnsembleData | None] = {}
        observations: dict[str, nws.ObservedDay | None] = {}
        for sid in stations_needed:
            station = STATIONS[sid]
            local_now = nws.local_now(station)
            ensembles[sid] = openmeteo.get_ensemble(
                conn, client, station,
                settings.forecast.ensemble_models,
                settings.forecast.cache_minutes,
                settings.staleness.forecast_max_age_hours,
                forecast_days=settings.strategy.max_lead_days + 3,
                today_local=local_now.date().isoformat(),
                from_hour=local_now.hour,
            )
            observations[sid] = _update_observations(
                conn, client, station, settings, local_now.date()
            )
        db.fill_calibration_actuals(conn)

        # 10-12. evaluate every parsed market
        candidates: list[tuple[rules.EntryContext, Decision, gamma.GammaMarket]] = []
        for market, claim in parsed:
            d = _evaluate_market(conn, cycle_id, client, settings, market, claim,
                                 ensembles, observations, bankroll, risk)
            report.decisions.append(d)
            if d.decision == "candidate":
                ctx = d._ctx  # type: ignore[attr-defined]
                candidates.append((ctx, d, market))
            else:
                _record(conn, cycle_id, d)

        # exits for open positions
        orders_placed += _evaluate_exits(conn, cycle_id, client, settings, executor,
                                         ensembles, observations, report)

        # 13-15. rank, cap, size, route
        candidates.sort(key=lambda t: rules.rank_key(t[0]), reverse=True)
        for ctx, d, market in candidates:
            er = ctx.edge_result
            if orders_placed >= risk.limits.max_positions_per_cycle:
                d.decision, d.skip_reason = "skip", "max_positions_per_cycle"
                _record(conn, cycle_id, d)
                continue
            sized = sizing.size_position(
                bankroll, er.edge, er.exec_price,
                settings.strategy.kelly_multiplier,
                risk.limits.max_position_frac,
                er.walk.cost_usd,
            )
            if sized is None:
                d.decision, d.skip_reason = "skip", "size_too_small"
                _record(conn, cycle_id, d)
                continue
            # hard risk gate immediately before the order
            reject = risk.check_order(sized.cost_usd, d.claim.city, bankroll, orders_placed)
            if reject:
                d.decision, d.skip_reason = "skip", f"risk:{reject}"
                _record(conn, cycle_id, d)
                continue
            intent = OrderIntent(
                market_id=market.id,
                token_id=er.token_id,
                side=er.side,
                price=sized.price,
                shares=sized.shares,
                cost_usd=sized.cost_usd,
                city=d.claim.city,
                station_id=d.claim.station_id,
                claim_json=d.claim.model_dump_json(),
                resolution_date=d.claim.resolution_date.isoformat(),
            )
            outcome = executor.open(intent, cycle_id)
            if outcome.ok:
                orders_placed += 1
                bankroll = executor.bankroll()
                report.bankroll = bankroll
                d.decision, d.cost_usd = "enter", sized.cost_usd
                log.info("ENTER %s %s $%.2f @ %.3f edge=%.3f conf=%.2f",
                         er.side, market.id, sized.cost_usd, sized.price, er.edge, ctx.confidence)
            else:
                d.decision, d.skip_reason = "skip", f"execution_failed:{outcome.detail}"
            _record(conn, cycle_id, d)

        # 16. finish cycle row
        open_count = len(db.get_open_positions(conn))
        if report.status == "ok" and any(n.startswith(("gamma_failed", "forecast")) for n in report.notes):
            report.status = "degraded"
        report.orders_placed = orders_placed
        db.finish_cycle(conn, cycle_id, report.status, bankroll, open_count,
                        orders_placed, "; ".join(report.notes)[:500] or None)

        # 17. heartbeat success
        if not dry_run:
            heartbeat.ping_success(settings.heartbeat)
        return report
    except Exception:
        db.finish_cycle(conn, cycle_id, "error", note="unhandled exception")
        if not dry_run:
            heartbeat.ping_fail(settings.heartbeat)
        raise
    finally:
        client.close()
        conn.close()


def _evaluate_market(conn, cycle_id, client, settings: Settings, market, claim,
                     ensembles, observations, bankroll, risk) -> Decision:
    d = Decision(market.id, market.question, "skip", claim=claim)
    station = get_station(claim.station_id)
    local_today = nws.local_now(station).date()
    lead_days = float((claim.resolution_date - local_today).days)
    d.lead_days = lead_days
    d.volume_24h = market.volume_24h

    if lead_days < 0:
        d.skip_reason = "already_resolved"
        return d
    if claim.metric not in ("high_temp", "low_temp"):
        d.skip_reason = f"metric_unsupported:{claim.metric}"
        return d

    ens = ensembles.get(claim.station_id)
    if ens is None:
        d.skip_reason = "forecast_unavailable_or_stale"
        return d
    members = ens.members_for(claim.resolution_date, claim.metric)

    calib = distribution.load_calibration(
        conn, claim.station_id, claim.metric, distribution.lead_bucket(lead_days)
    )
    obs = observations.get(claim.station_id)
    observed_value = None
    observed_current = None
    member_nows = None
    if lead_days == 0:
        if obs is None:
            # the observation IS the same-day signal; without it, fail closed
            d.skip_reason = "no_observation_data_for_same_day_market"
            return d
        observed_value = obs.high_f if claim.metric == "high_temp" else obs.low_f
        if observed_value is None:
            d.skip_reason = "no_observation_data_for_same_day_market"
            return d
        observed_current = obs.current_f
        member_nows = ens.now_for(claim.resolution_date)

    fv = distribution.fair_value(claim, members, lead_days, calib, observed_value,
                                 member_nows=member_nows, observed_current=observed_current)
    if fv is None:
        d.skip_reason = (f"too_few_ensemble_members:{len(members)}"
                         if len(members) < distribution.MIN_MEMBERS
                         else "fair_value_unavailable")
        return d
    d.fair_prob, d.confidence = fv.probability, fv.confidence

    # calibration snapshot for future (forecast, actual) pairs. Same-day
    # snapshots are skipped: remaining-day extremes are not a day-high
    # forecast and would pollute the calibration.
    if fv.forecast_mean is not None and lead_days >= 1:
        db.record_forecast_snapshot(
            conn, claim.station_id, claim.metric,
            distribution.lead_bucket(lead_days),
            claim.resolution_date.isoformat(),
            fv.forecast_mean, fv.forecast_std or 0.0,
        )

    # 11. orderbook -> executable price
    yes_token, no_token = market.yes_token_id(), market.no_token_id()
    if not yes_token:
        d.skip_reason = "no_token_ids"
        return d
    try:
        yes_book = clob.fetch_book(client, yes_token)
        no_book = clob.fetch_book(client, no_token) if no_token else None
    except Exception as exc:
        d.skip_reason = f"book_fetch_failed:{exc}"
        return d
    if yes_book.age_seconds() > settings.staleness.market_max_age_seconds:
        d.skip_reason = "market_data_stale"
        return d

    target_usd = max(1.0, bankroll * risk.limits.max_position_frac)
    er = compute_edge(fv.probability, yes_book, no_book, yes_token, no_token, target_usd)
    if er is None:
        d.skip_reason = "empty_orderbook"
        return d
    d.side, d.exec_price, d.edge = er.side, er.exec_price, er.edge
    d.market_price = er.market_implied_yes

    ctx = rules.EntryContext(
        edge_result=er,
        confidence=fv.confidence,
        lead_days=lead_days,
        volume_24h=market.volume_24h,
        has_position=db.has_position(conn, market.id),
        observed_decided=fv.observed_decided,
    )
    skip = rules.entry_skip_reason(ctx, settings.strategy)
    if skip:
        d.skip_reason = skip
        return d

    d.decision = "candidate"
    d._ctx = ctx  # type: ignore[attr-defined]
    return d


def _evaluate_exits(conn, cycle_id, client, settings: Settings, executor,
                    ensembles, observations, report: CycleReport) -> int:
    """Optional early exit when the edge decisively flips. Returns orders placed."""
    exits = 0
    for pos in db.get_open_positions(conn):
        if not pos["claim_json"]:
            continue
        claim = WeatherClaim.model_validate(json.loads(pos["claim_json"]))
        station = get_station(claim.station_id)
        if station is None:
            continue
        local_today = nws.local_now(station).date()
        lead_days = float((claim.resolution_date - local_today).days)
        if lead_days < 0:
            continue  # settlement handles it
        ens = ensembles.get(claim.station_id)
        if ens is None:
            continue  # stale forecast: hold, never act on missing data
        members = ens.members_for(claim.resolution_date, claim.metric)
        calib = distribution.load_calibration(
            conn, claim.station_id, claim.metric, distribution.lead_bucket(lead_days))
        obs = observations.get(claim.station_id)
        observed_value = None
        if lead_days == 0 and obs is not None:
            observed_value = obs.high_f if claim.metric == "high_temp" else obs.low_f
        fv = distribution.fair_value(claim, members, lead_days, calib, observed_value)
        if fv is None:
            continue
        fair_side = fv.probability if pos["side"] == "YES" else 1.0 - fv.probability
        try:
            book = clob.fetch_book(client, pos["token_id"]) if pos["token_id"] else None
        except Exception:
            continue
        if book is None or book.age_seconds() > settings.staleness.market_max_age_seconds:
            continue
        sell = clob.walk_sell(book, pos["shares"])
        if sell is None or not sell.filled:
            continue
        if rules.should_exit(fair_side, sell.avg_price, settings.strategy.exit_edge):
            intent = CloseIntent(
                market_id=pos["market_id"], token_id=pos["token_id"], side=pos["side"],
                price=sell.avg_price, shares=pos["shares"], cost_basis_usd=pos["cost_usd"],
                reason=f"edge_flip fair={fair_side:.3f} sell={sell.avg_price:.3f}",
            )
            outcome = executor.close(intent, cycle_id)
            if outcome.ok:
                exits += 1
                d = Decision(pos["market_id"], f"[exit] {pos['market_id']}", "exit",
                             fair_prob=fv.probability, exec_price=sell.avg_price,
                             side=pos["side"], claim=claim)
                report.decisions.append(d)
                _record(conn, cycle_id, d)
    return exits


def _reconcile_live(conn, executor, settings: Settings, report: CycleReport) -> None:
    try:
        remote = executor.remote_positions()
    except AttributeError:
        return
    except Exception as exc:
        log.error("reconciliation fetch failed: %s", exc)
        report.notes.append("reconcile_failed")
        return
    local = {str(p["token_id"]): p["market_id"] for p in db.get_open_positions(conn) if p["token_id"]}
    remote_ids = {r["token_id"] for r in remote}
    missing_local = remote_ids - set(local)
    missing_remote = set(local) - remote_ids
    if missing_local or missing_remote:
        msg = (f"POSITION DISCREPANCY local-only={sorted(missing_remote)} "
               f"remote-only={sorted(missing_local)}")
        log.error(msg)
        report.notes.append(msg)
        notify_urgent(settings.alerts, msg)
