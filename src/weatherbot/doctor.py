"""`weatherbot doctor`: verify every prerequisite before trusting the schedule.

Each check prints PASS / TODO (not configured yet) / FAIL (configured but
broken) with the exact remediation. Alert checks really send: a PASS for
Telegram means a test message arrived in your chat, and a PASS for
heartbeat means healthchecks.io just received a ping.

Exit code: 0 when nothing FAILs (TODOs are allowed — the bot runs in a
degraded-but-safe way without alerts), 1 otherwise.
"""

from __future__ import annotations

import os
import shutil
import sqlite3

import httpx

from weatherbot import db
from weatherbot.alerts import send_email, send_telegram
from weatherbot.config import Settings

PASS, TODO, FAIL = "PASS", "TODO", "FAIL"


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, status: str, name: str, detail: str = "") -> None:
        self.rows.append((status, name, detail))

    @property
    def failed(self) -> bool:
        return any(s == FAIL for s, _, _ in self.rows)

    def print(self) -> None:
        icons = {PASS: "✅", TODO: "⚠️ ", FAIL: "❌"}
        for status, name, detail in self.rows:
            line = f"{icons[status]} [{status}] {name}"
            if detail:
                line += f" — {detail}"
            print(line)
        todos = sum(1 for s, _, _ in self.rows if s == TODO)
        fails = sum(1 for s, _, _ in self.rows if s == FAIL)
        print(f"\n{len(self.rows)} checks: {len(self.rows) - todos - fails} pass, "
              f"{todos} todo, {fails} fail")


def _check_mode(settings: Settings, r: Report) -> None:
    if not settings.is_live:
        r.add(PASS, "trading mode", "paper")
        if os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip():
            r.add(PASS, "live credentials",
                  "present but inactive (paper mode) — run `weatherbot live-check` to verify them")
        return
    # live mode is a deliberate operator choice; verify its prerequisites
    r.add(PASS, "trading mode", "LIVE — real orders on scheduled cycles "
          "(requires the ack flag in the scheduler wrapper)")
    try:
        import py_clob_client  # noqa: F401, PLC0415
        r.add(PASS, "py-clob-client installed")
    except ImportError:
        r.add(FAIL, "py-clob-client installed", "run: uv sync --extra live")
    if not os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip():
        r.add(FAIL, "live private key", "POLYMARKET_PRIVATE_KEY missing from the key env file")
    if not os.environ.get("POLYMARKET_PROXY_ADDRESS", "").strip():
        r.add(FAIL, "live proxy address", "POLYMARKET_PROXY_ADDRESS missing from the key env file")
    r.add(TODO, "live path verification",
          "run `weatherbot live-check` to test auth + balance without placing orders")


def _check_db(settings: Settings, r: Report) -> None:
    try:
        conn = db.connect(settings.db_path)
        halted, reason = db.is_halted(conn)
        conn.close()
        if halted:
            r.add(FAIL, "database", f"HALTED ({reason}) — clear with `weatherbot clear-halt`")
        else:
            r.add(PASS, "database", settings.db_path)
    except sqlite3.Error as exc:
        r.add(FAIL, "database", f"{exc} — run `weatherbot init-db`")


def _check_market_data(client: httpx.Client, r: Report) -> None:
    try:
        resp = client.get("https://gamma-api.polymarket.com/markets",
                          params={"limit": 1}, timeout=15)
        resp.raise_for_status()
        r.add(PASS, "Polymarket Gamma API")
    except Exception as exc:
        r.add(FAIL, "Polymarket Gamma API", str(exc)[:120])
    try:
        resp = client.get("https://clob.polymarket.com/ok", timeout=15)
        r.add(PASS if resp.status_code < 500 else FAIL, "Polymarket CLOB API")
    except Exception as exc:
        r.add(FAIL, "Polymarket CLOB API", str(exc)[:120])


def _check_forecasts(settings: Settings, client: httpx.Client, r: Report) -> None:
    try:
        resp = client.get(
            "https://ensemble-api.open-meteo.com/v1/ensemble",
            params={"latitude": 40.78, "longitude": -73.88,
                    "hourly": "temperature_2m", "models": "gfs025",
                    "forecast_days": 1},
            timeout=30,
        )
        resp.raise_for_status()
        r.add(PASS, "Open-Meteo ensemble API")
    except Exception as exc:
        r.add(FAIL, "Open-Meteo ensemble API", str(exc)[:120])

    try:
        resp = client.get(
            "https://aviationweather.gov/api/data/metar",
            params={"ids": "EGLC", "format": "json", "hours": 2},
            headers={"User-Agent": settings.forecast.nws_user_agent},
            timeout=20,
        )
        resp.raise_for_status()
        r.add(PASS, "AviationWeather METAR API (international obs)")
    except Exception as exc:
        r.add(FAIL, "AviationWeather METAR API (international obs)", str(exc)[:120])

    ua = settings.forecast.nws_user_agent
    if "set-your-contact-here" in ua:
        r.add(TODO, "NWS User-Agent",
              "still the placeholder — put a real contact in config.yaml "
              "(forecast.nws_user_agent); NWS may throttle anonymous agents")
    try:
        resp = client.get("https://api.weather.gov/stations/KLGA/observations/latest",
                          headers={"User-Agent": ua}, timeout=20)
        resp.raise_for_status()
        r.add(PASS, "NWS observations API")
    except Exception as exc:
        r.add(FAIL, "NWS observations API", str(exc)[:120])


def _check_heartbeat(settings: Settings, client: httpx.Client, r: Report) -> None:
    url = settings.heartbeat.url
    if not url:
        r.add(TODO, "heartbeat",
              "no healthchecks.io URL in config.yaml — create a check "
              "(period 20 min, grace 30 min) and paste its ping URL under heartbeat.url")
        return
    try:
        resp = client.get(url, timeout=settings.heartbeat.timeout_seconds)
        resp.raise_for_status()
        r.add(PASS, "heartbeat", "test ping delivered — it should show on your healthchecks dashboard")
    except Exception as exc:
        r.add(FAIL, "heartbeat", f"ping to configured URL failed: {str(exc)[:120]}")


def _check_telegram(settings: Settings, r: Report) -> None:
    tg = settings.alerts.telegram
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not tg.enabled:
        r.add(TODO, "telegram", "disabled in config.yaml")
        return
    if not token or not tg.chat_id:
        missing = []
        if not token:
            missing.append("TELEGRAM_BOT_TOKEN in the key file (create a bot via @BotFather)")
        if not tg.chat_id:
            missing.append("alerts.telegram.chat_id in config.yaml (message the bot once, "
                           "then read chat id from api.telegram.org/bot<token>/getUpdates)")
        r.add(TODO, "telegram", "; ".join(missing))
        return
    if send_telegram(settings.alerts, "weatherbot doctor: test message ✔"):
        r.add(PASS, "telegram", "test message sent — check your chat")
    else:
        r.add(FAIL, "telegram", "configured but sending failed — check token/chat_id")


def _check_email(settings: Settings, r: Report) -> None:
    em = settings.alerts.email
    if not em.enabled:
        r.add(TODO, "email", "disabled in config.yaml")
        return
    missing = []
    if not (em.smtp_host and em.from_addr and em.to_addr):
        missing.append("smtp_host/from_addr/to_addr in config.yaml")
    if not os.environ.get("SMTP_USERNAME", "").strip():
        missing.append("SMTP_USERNAME in the key file")
    if not os.environ.get("SMTP_PASSWORD", "").strip():
        missing.append("SMTP_PASSWORD in the key file (Gmail: use an App Password)")
    if missing:
        r.add(TODO, "email", "; ".join(missing))
        return
    if send_email(settings.alerts, "weatherbot doctor", "Test email — alerts are working."):
        r.add(PASS, "email", "test email sent — check your inbox")
    else:
        r.add(FAIL, "email", "configured but sending failed — check SMTP credentials")


def _check_review_cli(r: Report) -> None:
    if shutil.which("claude"):
        r.add(PASS, "claude CLI (nightly review)")
    else:
        r.add(TODO, "claude CLI (nightly review)",
              "not on PATH — the trading loop works without it, but the 6am "
              "review job will fail until it is installed and logged in")


def _check_key_file(r: Report) -> None:
    key_file = os.environ.get("WEATHERBOT_KEY_FILE") or os.path.expanduser("~/.weatherbot.env")
    if not os.path.exists(key_file):
        r.add(TODO, "key env file",
              f"{key_file} not found — copy .env.example there "
              "(only needed for alerts now; trading secrets only for live)")
        return
    if os.name != "nt":
        perms = os.stat(key_file).st_mode & 0o777
        if perms & 0o077:
            r.add(FAIL, "key env file", f"{key_file} is {oct(perms)} — run: chmod 600 {key_file}")
            return
    r.add(PASS, "key env file", key_file)


def run_doctor(settings: Settings) -> int:
    r = Report()
    print("weatherbot doctor\n")
    _check_mode(settings, r)
    _check_db(settings, r)
    _check_key_file(r)
    with httpx.Client(follow_redirects=True) as client:
        _check_market_data(client, r)
        _check_forecasts(settings, client, r)
        _check_heartbeat(settings, client, r)
    _check_telegram(settings, r)
    _check_email(settings, r)
    _check_review_cli(r)
    print()
    r.print()
    if r.failed:
        print("\nFix the FAIL items before trusting the schedule.")
        return 1
    todos = any(s == TODO for s, _, _ in r.rows)
    if todos:
        print("\nTODO items are safe to leave for now (the bot fails closed "
              "without them) but you won't get alerts until they're done.")
    else:
        print("\nAll checks pass. The bot is ready to run on schedule.")
    return 0
