"""Operator alerts: Telegram for urgent events, email for summaries.

Secrets (bot token, SMTP password) come from the environment only and are
registered with the log redactor at startup. Alert failures are logged and
swallowed — alerting must never break the trading loop.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

import httpx

from weatherbot.config import AlertsConfig

log = logging.getLogger(__name__)


def send_telegram(cfg: AlertsConfig, text: str) -> bool:
    tg = cfg.telegram
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not (tg.enabled and token and tg.chat_id):
        return False
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": tg.chat_id, "text": text[:4000]},
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:
        log.warning("telegram send failed: %s", exc)
        return False


def send_email(cfg: AlertsConfig, subject: str, body: str) -> bool:
    em = cfg.email
    username = os.environ.get("SMTP_USERNAME", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").strip()
    if not (em.enabled and em.smtp_host and em.from_addr and em.to_addr and username and password):
        return False
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = em.from_addr
        msg["To"] = em.to_addr
        msg.set_content(body)
        with smtplib.SMTP(em.smtp_host, em.smtp_port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(username, password)
            smtp.send_message(msg)
        return True
    except Exception as exc:
        log.warning("email send failed: %s", exc)
        return False


def notify_urgent(cfg: AlertsConfig, text: str) -> None:
    """Halts, kill switches, reconciliation discrepancies: both channels."""
    sent_tg = send_telegram(cfg, f"🚨 weatherbot: {text}")
    sent_mail = send_email(cfg, "weatherbot ALERT", text)
    if not (sent_tg or sent_mail):
        log.error("URGENT ALERT (no channel configured/working): %s", text)
