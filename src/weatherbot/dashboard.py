"""Local dashboard: `weatherbot dashboard` serves a live view of the bot.

Binds to 127.0.0.1 only (never exposed to the network), reads the database
strictly read-only per request, and serves a single self-contained HTML page
that polls /data every 30 seconds: equity curve, KPI row, open positions,
and settled trade history.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger(__name__)

HTML_PATH = Path(__file__).with_name("dashboard.html")

_QUESTION = ("(SELECT question FROM evaluations e WHERE e.market_id = %s.market_id "
             "ORDER BY e.id DESC LIMIT 1)")


def build_data(db_path: str | Path) -> dict:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        equity = [
            dict(r)
            for r in conn.execute(
                """SELECT c.finished_at AS t, c.bankroll AS cash,
                          c.bankroll + (
                            SELECT COALESCE(SUM(p.cost_usd), 0) FROM positions p
                            WHERE p.opened_at <= c.finished_at
                              AND (p.closed_at IS NULL OR p.closed_at > c.finished_at)
                          ) AS equity
                   FROM cycles c
                   WHERE c.bankroll IS NOT NULL AND c.finished_at IS NOT NULL
                   ORDER BY c.id"""
            )
        ]
        open_positions = [
            dict(r)
            for r in conn.execute(
                f"""SELECT market_id, city, side, shares, avg_price, cost_usd,
                           resolution_date, opened_at, {_QUESTION % 'positions'} AS question
                    FROM positions WHERE status = 'open' ORDER BY opened_at DESC"""
            )
        ]
        settled = [
            dict(r)
            for r in conn.execute(
                f"""SELECT market_id, city, side, cost_usd, outcome, pnl_usd, closed_at,
                           {_QUESTION % 'positions'} AS question
                    FROM positions WHERE status != 'open'
                    ORDER BY closed_at DESC LIMIT 50"""
            )
        ]
        halted, halt_reason = False, None
        row = conn.execute("SELECT halted, reason FROM halt WHERE id = 1").fetchone()
        if row:
            halted, halt_reason = bool(row["halted"]), row["reason"]
        cash_row = conn.execute("SELECT bankroll FROM paper_account WHERE id = 1").fetchone()
        cash = float(cash_row["bankroll"]) if cash_row else None
        last_cycle = conn.execute(
            "SELECT id, started_at, status, mode FROM cycles ORDER BY id DESC LIMIT 1"
        ).fetchone()
        totals = conn.execute(
            """SELECT COALESCE(SUM(pnl_usd), 0) AS realized,
                      SUM(CASE WHEN outcome = 'won' THEN 1 ELSE 0 END) AS wins,
                      SUM(CASE WHEN outcome = 'lost' THEN 1 ELSE 0 END) AS losses
               FROM positions WHERE status != 'open'"""
        ).fetchone()
        open_cost = sum(p["cost_usd"] for p in open_positions)
        return {
            "equity_series": equity,
            "open_positions": open_positions,
            "settled": settled,
            "summary": {
                "cash": cash,
                "open_cost": round(open_cost, 2),
                "equity": round((cash or 0) + open_cost, 2) if cash is not None else None,
                "realized_pnl": round(totals["realized"] or 0, 2),
                "wins": totals["wins"] or 0,
                "losses": totals["losses"] or 0,
                "halted": halted,
                "halt_reason": halt_reason,
                "last_cycle": dict(last_cycle) if last_cycle else None,
            },
        }
    finally:
        conn.close()


def make_handler(db_path: str) -> type[BaseHTTPRequestHandler]:
    html = HTML_PATH.read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib naming
            if self.path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", html)
            elif self.path == "/data":
                try:
                    body = json.dumps(build_data(db_path), default=str).encode()
                    self._send(200, "application/json", body)
                except Exception as exc:
                    self._send(500, "application/json",
                               json.dumps({"error": str(exc)[:200]}).encode())
            else:
                self._send(404, "text/plain", b"not found")

        def _send(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):  # quiet the per-request noise
            pass

    return Handler


def run_dashboard(db_path: str, port: int = 8787, open_browser: bool = True) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(db_path))
    url = f"http://127.0.0.1:{port}"
    print(f"weatherbot dashboard: {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\ndashboard stopped")
    finally:
        server.server_close()
