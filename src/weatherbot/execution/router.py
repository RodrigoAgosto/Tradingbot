"""Executor selection. Paper unless every live precondition holds."""

from __future__ import annotations

import logging
import sqlite3

from weatherbot.config import Settings
from weatherbot.execution.paper import PaperExecutor

log = logging.getLogger(__name__)


def get_executor(conn: sqlite3.Connection, settings: Settings, live_ack: bool):
    """Return the executor for this run.

    Live requires BOTH TRADING_MODE=live and --i-understand-this-is-live.
    A live config without the flag is a refusal (raises), not a silent
    downgrade — a half-configured live deployment should be loud.
    """
    if settings.is_live:
        from weatherbot.execution.live import LiveExecutor  # noqa: PLC0415

        return LiveExecutor(conn, settings, live_ack)
    if live_ack:
        log.warning("--i-understand-this-is-live passed but TRADING_MODE != live; staying in paper mode")
    return PaperExecutor(conn, settings.paper.starting_bankroll)
