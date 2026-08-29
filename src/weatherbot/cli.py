"""Command-line entrypoints.

    weatherbot cycle [--dry-run] [--i-understand-this-is-live] [--config PATH]
    weatherbot status
    weatherbot halt --reason TEXT
    weatherbot clear-halt
    weatherbot init-db

Every command sets up redacted logging first. Any unhandled exception is
logged (with key redaction), the failure heartbeat fires, and the process
exits non-zero — the missed success ping is the alert.
"""

from __future__ import annotations

import argparse
import logging
import sys

from weatherbot import db, logutil
from weatherbot.config import load_key_env_file, load_settings

log = logging.getLogger("weatherbot")


def _print_decision_table(report) -> None:
    print(f"\nmode status={report.status} bankroll=${report.bankroll:.2f} "
          f"orders={report.orders_placed}")
    header = f"{'decision':10} {'side':4} {'fair':>6} {'exec':>6} {'edge':>7} {'conf':>5} {'lead':>4}  market / reason"
    print(header)
    print("-" * len(header))
    for d in report.decisions:
        fair = f"{d.fair_prob:.3f}" if d.fair_prob is not None else "-"
        ex = f"{d.exec_price:.3f}" if d.exec_price is not None else "-"
        edge = f"{d.edge:+.3f}" if d.edge is not None else "-"
        conf = f"{d.confidence:.2f}" if d.confidence is not None else "-"
        lead = f"{d.lead_days:.0f}d" if d.lead_days is not None else "-"
        label = d.question[:60] if d.decision != "skip" else f"{d.question[:38]} [{d.skip_reason}]"
        print(f"{d.decision:10} {d.side or '-':4} {fair:>6} {ex:>6} {edge:>7} {conf:>5} {lead:>4}  {label}")
    for note in report.notes:
        print(f"note: {note}")


def cmd_cycle(args) -> int:
    from weatherbot.cycle import run_cycle  # import late: keeps CLI fast for admin cmds

    settings = load_settings(args.config)
    if settings.is_live and not args.i_understand_this_is_live:
        log.error("TRADING_MODE=live but --i-understand-this-is-live was not passed; refusing to run.")
        return 2
    if args.dry_run:
        print("=== DRY RUN: no DB writes, no orders leave this process ===")
    report = run_cycle(settings, dry_run=args.dry_run, live_ack=args.i_understand_this_is_live)
    if args.dry_run:
        _print_decision_table(report)
    else:
        entered = sum(1 for d in report.decisions if d.decision == "enter")
        skipped = sum(1 for d in report.decisions if d.decision == "skip")
        log.info("cycle done status=%s evaluated=%d entered=%d skipped=%d bankroll=$%.2f",
                 report.status, len(report.decisions), entered, skipped, report.bankroll)
    return 0


def cmd_status(args) -> int:
    settings = load_settings(args.config)
    conn = db.connect(settings.db_path)
    halted, reason = db.is_halted(conn)
    print(f"mode: {settings.mode}")
    print(f"halted: {halted}" + (f" ({reason})" if reason else ""))
    if settings.mode == "paper":
        print(f"paper bankroll: ${db.get_paper_bankroll(conn, settings.paper.starting_bankroll):.2f}")
    positions = db.get_open_positions(conn)
    print(f"open positions: {len(positions)}")
    for p in positions:
        print(f"  {p['market_id']} {p['side']} {p['shares']:.1f} sh @ {p['avg_price']:.3f} "
              f"(${p['cost_usd']:.2f}) {p['city']} res={p['resolution_date']}")
    last = conn.execute("SELECT * FROM cycles ORDER BY id DESC LIMIT 1").fetchone()
    if last:
        print(f"last cycle: #{last['id']} {last['started_at']} status={last['status']}")
    conn.close()
    return 0


def cmd_halt(args) -> int:
    settings = load_settings(args.config)
    conn = db.connect(settings.db_path)
    db.set_halt(conn, f"manual: {args.reason}")
    print(f"halted: {args.reason}")
    conn.close()
    return 0


def cmd_clear_halt(args) -> int:
    settings = load_settings(args.config)
    conn = db.connect(settings.db_path)
    halted, reason = db.is_halted(conn)
    if not halted:
        print("not halted; nothing to clear")
    else:
        db.clear_halt(conn)
        print(f"halt cleared (was: {reason})")
    conn.close()
    return 0


def cmd_doctor(args) -> int:
    from weatherbot.doctor import run_doctor

    return run_doctor(load_settings(args.config))


def cmd_reset_paper(args) -> int:
    settings = load_settings(args.config)
    if settings.is_live:
        log.error("refusing to reset while TRADING_MODE=live")
        return 2
    conn = db.connect(settings.db_path)
    counts = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("positions", "orders", "evaluations", "cycles", "daily_bankroll")
    }
    print("This will DELETE the paper trading ledger:")
    for table, n in counts.items():
        print(f"  {table:15} {n} rows")
    print(f"and set the paper bankroll to ${args.bankroll:.2f}.")
    print("Kept: observations, calibration history, forecast cache, halt state.")
    if not args.yes:
        print("\nDry preview only — re-run with --yes to actually reset.")
        conn.close()
        return 0
    for table in ("positions", "orders", "evaluations", "cycles", "daily_bankroll"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    db.set_paper_bankroll(conn, args.bankroll)
    conn.close()
    print(f"\nreset complete: fresh ledger, paper bankroll ${args.bankroll:.2f}")
    return 0


def cmd_live_check(args) -> int:
    from weatherbot.execution.live import live_preflight

    return live_preflight()


def cmd_dashboard(args) -> int:
    from weatherbot.dashboard import run_dashboard

    settings = load_settings(args.config)
    run_dashboard(settings.db_path, port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_init_db(args) -> int:
    settings = load_settings(args.config)
    conn = db.connect(settings.db_path)
    conn.close()
    print(f"database ready: {settings.db_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Load secrets from the key file BEFORE logging setup, so they are
    # registered with the redactor from the very first log line.
    load_key_env_file()
    logutil.setup_logging()

    parser = argparse.ArgumentParser(prog="weatherbot")
    parser.add_argument("--config", default="config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    p_cycle = sub.add_parser("cycle", help="run one trading cycle and exit")
    p_cycle.add_argument("--dry-run", action="store_true",
                         help="full cycle against live data, zero DB writes, prints decisions")
    p_cycle.add_argument("--i-understand-this-is-live", action="store_true",
                         help="required (with TRADING_MODE=live) for real orders")
    p_cycle.set_defaults(func=cmd_cycle)

    sub.add_parser("status", help="print halt state, bankroll, open positions").set_defaults(func=cmd_status)

    p_halt = sub.add_parser("halt", help="manually halt all trading")
    p_halt.add_argument("--reason", required=True)
    p_halt.set_defaults(func=cmd_halt)

    sub.add_parser("clear-halt", help="clear a halt (operator only)").set_defaults(func=cmd_clear_halt)
    sub.add_parser("init-db", help="create/migrate the database").set_defaults(func=cmd_init_db)
    sub.add_parser(
        "doctor",
        help="verify every prerequisite (sends test alerts, pings heartbeat, checks APIs)",
    ).set_defaults(func=cmd_doctor)

    p_reset = sub.add_parser(
        "reset-paper",
        help="wipe the paper ledger (positions/orders/history) and set a fresh bankroll",
    )
    p_reset.add_argument("--bankroll", type=float, default=1000.0,
                         help="fresh paper bankroll (default 1000)")
    p_reset.add_argument("--yes", action="store_true",
                         help="actually reset (without this, prints a preview only)")
    p_reset.set_defaults(func=cmd_reset_paper)

    sub.add_parser(
        "live-check",
        help="verify the full live-trading path (auth, balance) WITHOUT placing orders",
    ).set_defaults(func=cmd_live_check)

    p_dash = sub.add_parser(
        "dashboard", help="serve a live local dashboard (equity chart, positions, trades)"
    )
    p_dash.add_argument("--port", type=int, default=8787)
    p_dash.add_argument("--no-browser", action="store_true",
                        help="don't auto-open the browser")
    p_dash.set_defaults(func=cmd_dashboard)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception:
        # Redacted traceback via the logging formatter; never a raw print.
        log.exception("unhandled exception in %s", args.command)
        return 1


if __name__ == "__main__":
    sys.exit(main())
