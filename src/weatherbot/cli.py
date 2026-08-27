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
from weatherbot.config import load_settings

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


def cmd_init_db(args) -> int:
    settings = load_settings(args.config)
    conn = db.connect(settings.db_path)
    conn.close()
    print(f"database ready: {settings.db_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
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

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception:
        # Redacted traceback via the logging formatter; never a raw print.
        log.exception("unhandled exception in %s", args.command)
        return 1


if __name__ == "__main__":
    sys.exit(main())
