"""Paper executor: does everything live would do except submit.

Fills are simulated at the walked executable price (the same number live
would target), debited from the simulated bankroll, and recorded exactly
like live fills so the DB, risk checks and review layer behave identically
in both modes.
"""

from __future__ import annotations

import logging
import sqlite3

from weatherbot import db
from weatherbot.execution.types import CloseIntent, ExecutionReport, OrderIntent

log = logging.getLogger(__name__)


class PaperExecutor:
    mode = "paper"

    def __init__(self, conn: sqlite3.Connection, starting_bankroll: float):
        self.conn = conn
        self.starting_bankroll = starting_bankroll

    def bankroll(self) -> float:
        return db.get_paper_bankroll(self.conn, self.starting_bankroll)

    def open(self, intent: OrderIntent, cycle_id: int | None) -> ExecutionReport:
        bankroll = self.bankroll()
        if intent.cost_usd > bankroll:
            db.record_order(self.conn, cycle_id, intent.as_order_row(self.mode, "rejected", "insufficient_bankroll"))
            return ExecutionReport(ok=False, detail="insufficient_bankroll")

        db.record_order(self.conn, cycle_id, intent.as_order_row(self.mode, "filled"))
        db.open_position(
            self.conn,
            {
                "market_id": intent.market_id,
                "token_id": intent.token_id,
                "city": intent.city,
                "station_id": intent.station_id,
                "side": intent.side,
                "shares": intent.shares,
                "avg_price": intent.price,
                "cost_usd": intent.cost_usd,
                "claim_json": intent.claim_json,
                "resolution_date": intent.resolution_date,
            },
        )
        db.set_paper_bankroll(self.conn, bankroll - intent.cost_usd)
        log.info(
            "paper fill: %s %s %.1f sh @ %.3f ($%.2f) market=%s",
            intent.side, intent.city, intent.shares, intent.price, intent.cost_usd, intent.market_id,
        )
        return ExecutionReport(ok=True, fill_price=intent.price)

    def close(self, intent: CloseIntent, cycle_id: int | None) -> ExecutionReport:
        proceeds = intent.shares * intent.price
        db.record_order(
            self.conn,
            cycle_id,
            {
                "market_id": intent.market_id,
                "token_id": intent.token_id,
                "side": intent.side,
                "action": "close",
                "price": intent.price,
                "shares": intent.shares,
                "cost_usd": proceeds,
                "mode": self.mode,
                "status": "filled",
                "detail": intent.reason,
            },
        )
        pnl = proceeds - intent.cost_basis_usd
        db.close_position(self.conn, intent.market_id, outcome="exited", pnl_usd=pnl)
        db.set_paper_bankroll(self.conn, self.bankroll() + proceeds)
        log.info("paper exit: %s %.1f sh @ %.3f pnl=$%.2f (%s)",
                 intent.market_id, intent.shares, intent.price, pnl, intent.reason)
        return ExecutionReport(ok=True, fill_price=intent.price)

    def settle(self, market_id: str, won: bool, shares: float, cost_basis_usd: float) -> float:
        """Resolve a held position: winners pay $1/share, losers $0."""
        proceeds = shares if won else 0.0
        pnl = proceeds - cost_basis_usd
        db.close_position(
            self.conn, market_id, outcome="won" if won else "lost",
            pnl_usd=pnl, status="resolved",
        )
        if proceeds:
            db.set_paper_bankroll(self.conn, self.bankroll() + proceeds)
        log.info("paper settle: %s %s pnl=$%.2f", market_id, "WON" if won else "LOST", pnl)
        return pnl
