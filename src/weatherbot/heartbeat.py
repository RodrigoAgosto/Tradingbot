"""healthchecks.io heartbeat pings.

A cycle pings /start at the beginning and the bare URL on success. An
unhandled crash never sends the success ping, so the check times out and
alerts. Ping failures are logged but never break the cycle.
"""

from __future__ import annotations

import logging

import httpx

from weatherbot.config import HeartbeatConfig

log = logging.getLogger(__name__)


def _ping(cfg: HeartbeatConfig, suffix: str) -> None:
    if not cfg.url:
        return
    try:
        httpx.get(cfg.url.rstrip("/") + suffix, timeout=cfg.timeout_seconds)
    except Exception as exc:
        log.warning("heartbeat ping%s failed: %s", suffix or " (success)", exc)


def ping_start(cfg: HeartbeatConfig) -> None:
    _ping(cfg, "/start")


def ping_success(cfg: HeartbeatConfig) -> None:
    _ping(cfg, "")


def ping_fail(cfg: HeartbeatConfig) -> None:
    _ping(cfg, "/fail")
