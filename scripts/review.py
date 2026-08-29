#!/usr/bin/env python3
"""Nightly LLM review wrapper — the ONLY place an LLM touches this system.

This script extracts the last 24 h of data from SQLite itself (read-only
URI) into a plain text snapshot file, then shells out to `claude -p` with
ONLY the Read tool. The LLM never gets database or shell access of any
kind — it reads the snapshot, writes the analysis, and the summary goes to
Telegram and email. It never touches the wallet, never writes to the DB,
and never runs inside the trading loop.

Run at 6am by launchd (macOS) or Task Scheduler via ops/run_review.ps1
(Windows).
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from weatherbot import logutil  # noqa: E402
from weatherbot.alerts import send_email, send_telegram  # noqa: E402
from weatherbot.config import load_key_env_file, load_settings  # noqa: E402

log = logging.getLogger("review")


SNAPSHOT = REPO / ".review_snapshot.md"


def _rows(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def build_snapshot(db_path: Path) -> str:
    """Extract everything the review needs into plain text, read-only."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    out: list[str] = ["# weatherbot data snapshot (last 24h unless noted)\n"]

    def section(title, sql, params=(), fmt=None):
        out.append(f"\n## {title}\n")
        try:
            rows = _rows(conn, sql, params)
        except sqlite3.Error as exc:
            out.append(f"(query failed: {exc})\n")
            return
        if not rows:
            out.append("(none)\n")
            return
        for r in rows:
            if fmt:
                out.append(fmt(r) + "\n")
            else:
                out.append(json.dumps(dict(r), default=str) + "\n")

    section("halt state", "SELECT halted, reason, halted_at, cleared_at FROM halt WHERE id=1")
    section("bankroll (cash)", "SELECT bankroll, updated_at FROM paper_account WHERE id=1")
    section(
        "cycles last 24h",
        "SELECT status, COUNT(*) n, SUM(orders_placed) orders FROM cycles "
        "WHERE started_at > datetime('now','-1 day') GROUP BY status",
    )
    section(
        "cycle notes last 24h (non-empty)",
        "SELECT started_at, status, note FROM cycles "
        "WHERE started_at > datetime('now','-1 day') AND note IS NOT NULL AND note != ''",
    )
    section(
        "open positions",
        "SELECT market_id, city, side, shares, avg_price, cost_usd, resolution_date "
        "FROM positions WHERE status='open'",
    )
    section(
        "positions closed/resolved last 24h",
        "SELECT market_id, city, side, cost_usd, outcome, pnl_usd, closed_at "
        "FROM positions WHERE closed_at > datetime('now','-1 day')",
    )
    section(
        "entry-decision evaluations for those markets (fair_prob at entry)",
        "SELECT e.market_id, e.question, e.side, e.fair_prob, e.confidence, e.exec_price, e.edge "
        "FROM evaluations e WHERE e.decision='enter' "
        "AND e.market_id IN (SELECT market_id FROM positions WHERE closed_at > datetime('now','-1 day'))",
    )
    section(
        "skip reasons last 24h",
        "SELECT skip_reason, COUNT(*) n FROM evaluations "
        "WHERE created_at > datetime('now','-1 day') AND decision='skip' "
        "GROUP BY skip_reason ORDER BY n DESC",
    )
    section(
        "one example question per top skip reason",
        "SELECT skip_reason, MIN(question) example FROM evaluations "
        "WHERE created_at > datetime('now','-1 day') AND decision='skip' "
        "GROUP BY skip_reason ORDER BY COUNT(*) DESC LIMIT 10",
    )
    section(
        "calibration pairs per station (forecast vs actual, all time)",
        "SELECT station_id, metric, lead_bucket, COUNT(actual) n_resolved, "
        "ROUND(AVG(forecast_mean - actual), 2) mean_error "
        "FROM calibration_obs GROUP BY station_id, metric, lead_bucket",
    )
    section(
        "observations last 3 days",
        "SELECT station_id, day, high_f AS high, low_f AS low, final FROM observations "
        "WHERE day > date('now','-3 day') ORDER BY station_id, day",
    )
    conn.close()
    return "".join(out)


def run_review() -> str:
    settings = load_settings(REPO / "config.yaml")
    SNAPSHOT.write_text(build_snapshot(REPO / settings.db_path), encoding="utf-8")

    prompt = (REPO / "prompts" / "nightly_review.md").read_text()
    prompt = prompt.replace("__SNAPSHOT_PATH__", str(SNAPSHOT))
    # shutil.which resolves claude.cmd/.exe on Windows, where a bare
    # subprocess name lookup would fail.
    claude_bin = shutil.which("claude")
    if not claude_bin:
        raise RuntimeError("claude CLI not found on PATH")
    cmd = [
        claude_bin,
        "-p", prompt,
        "--output-format", "json",
        "--allowedTools", "Read",
        "--add-dir", str(REPO),
    ]
    # Deliberately NO --dangerously-skip-permissions: anything beyond the
    # allowed read-only tools stays blocked.
    proc = subprocess.run(
        cmd, cwd=REPO, capture_output=True, text=True, timeout=1800,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude review failed rc={proc.returncode}: {proc.stderr[:500]}")
    payload = json.loads(proc.stdout)
    return payload.get("result", "")


def extract_telegram_summary(text: str) -> str:
    marker = "TELEGRAM SUMMARY:"
    if marker in text:
        return text.split(marker, 1)[1].strip()[:3900]
    return text.strip()[:3900]


def main() -> int:
    load_key_env_file()
    logutil.setup_logging()
    settings = load_settings(REPO / "config.yaml")
    try:
        full = run_review()
    except Exception:
        log.exception("nightly review failed")
        send_telegram(settings.alerts, "⚠️ weatherbot nightly review FAILED — check logs")
        return 1

    summary = extract_telegram_summary(full)
    sent_tg = send_telegram(settings.alerts, f"🌤 weatherbot nightly review\n\n{summary}")
    sent_mail = send_email(settings.alerts, "weatherbot nightly review", full)
    log.info("review sent telegram=%s email=%s", sent_tg, sent_mail)
    return 0 if (sent_tg or sent_mail) else 1


if __name__ == "__main__":
    sys.exit(main())
