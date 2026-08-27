#!/usr/bin/env python3
"""Nightly LLM review wrapper — the ONLY place an LLM touches this system.

Shells out to `claude -p` with read-only tools (Read + sqlite3), parses the
JSON result, and sends the summary to Telegram and email. It never touches
the wallet, never writes to the DB, and never runs inside the trading loop.

Run by launchd at 6am (see ops/com.rodrigo.weatherbot.review.plist).
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from weatherbot import logutil  # noqa: E402
from weatherbot.alerts import send_email, send_telegram  # noqa: E402
from weatherbot.config import load_settings  # noqa: E402

log = logging.getLogger("review")


def run_review() -> str:
    prompt = (REPO / "prompts" / "nightly_review.md").read_text()
    # shutil.which resolves claude.cmd/.exe on Windows, where a bare
    # subprocess name lookup would fail.
    claude_bin = shutil.which("claude")
    if not claude_bin:
        raise RuntimeError("claude CLI not found on PATH")
    cmd = [
        claude_bin,
        "-p", prompt,
        "--output-format", "json",
        "--allowedTools", "Read,Bash(sqlite3:*)",
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
