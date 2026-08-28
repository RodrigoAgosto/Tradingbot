"""Hard risk limits and kill switches.

Two layers:
  1. CODE CEILINGS (module constants below). Config may tighten a limit but
     can never loosen it past these — effective limit = min(config, ceiling).
  2. Per-order checks run IMMEDIATELY before every order is routed, against
     the live DB state, not values computed earlier in the cycle.

The daily-loss kill switch and the absolute bankroll floor write a
persistent HALTED row (db.halt) that survives restarts and is only cleared
by the operator via `weatherbot clear-halt`.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from weatherbot import db
from weatherbot.config import RiskConfig

log = logging.getLogger(__name__)

# Code ceilings — config cannot exceed these. Changing them requires a commit.
CEIL_MAX_POSITION_FRAC = 0.05
CEIL_MAX_TOTAL_EXPOSURE_FRAC = 0.40
CEIL_MAX_OPEN_POSITIONS = 8
CEIL_MAX_CITY_EXPOSURE_FRAC = 0.15
CEIL_MAX_POSITIONS_PER_CYCLE = 2
FLOOR_DAILY_LOSS_FRAC = 0.15      # config may be stricter (smaller), never looser
FLOOR_BANKROLL_USD = 25.0         # config may be stricter (higher), never lower


@dataclass
class EffectiveLimits:
    max_position_frac: float
    max_total_exposure_frac: float
    max_open_positions: int
    max_city_exposure_frac: float
    max_positions_per_cycle: int
    daily_loss_frac: float
    bankroll_floor_usd: float

    @classmethod
    def from_config(cls, cfg: RiskConfig) -> "EffectiveLimits":
        return cls(
            max_position_frac=min(cfg.max_position_frac, CEIL_MAX_POSITION_FRAC),
            max_total_exposure_frac=min(cfg.max_total_exposure_frac, CEIL_MAX_TOTAL_EXPOSURE_FRAC),
            max_open_positions=min(cfg.max_open_positions, CEIL_MAX_OPEN_POSITIONS),
            max_city_exposure_frac=min(cfg.max_city_exposure_frac, CEIL_MAX_CITY_EXPOSURE_FRAC),
            max_positions_per_cycle=min(cfg.max_positions_per_cycle, CEIL_MAX_POSITIONS_PER_CYCLE),
            daily_loss_frac=min(cfg.daily_loss_frac, FLOOR_DAILY_LOSS_FRAC),
            bankroll_floor_usd=max(cfg.bankroll_floor_usd, FLOOR_BANKROLL_USD),
        )


class RiskManager:
    def __init__(self, conn: sqlite3.Connection, cfg: RiskConfig):
        self.conn = conn
        self.limits = EffectiveLimits.from_config(cfg)

    # --- kill switches ------------------------------------------------------

    def equity(self, cash: float) -> float:
        """Cash plus the cost basis of open positions. Kill switches measure
        EQUITY, not cash: moving cash into a position is not a loss, and a
        false daily-loss halt every time the book fills would train the
        operator to ignore real halts."""
        open_cost = sum(p["cost_usd"] for p in db.get_open_positions(self.conn))
        return cash + open_cost

    def check_kill_switches(self, bankroll: float, day_start_bankroll: float) -> str | None:
        """Halts trading entirely when breached. Both arguments are EQUITY
        values (see equity()). Returns the halt reason."""
        lim = self.limits
        if bankroll < lim.bankroll_floor_usd:
            reason = f"bankroll_floor: bankroll ${bankroll:.2f} < ${lim.bankroll_floor_usd:.2f}"
            db.set_halt(self.conn, reason)
            log.error("KILL SWITCH: %s", reason)
            return reason
        if day_start_bankroll > 0:
            loss = (day_start_bankroll - bankroll) / day_start_bankroll
            if loss >= lim.daily_loss_frac:
                reason = (
                    f"daily_loss: down {loss:.1%} from day-start ${day_start_bankroll:.2f} "
                    f"(limit {lim.daily_loss_frac:.0%})"
                )
                db.set_halt(self.conn, reason)
                log.error("KILL SWITCH: %s", reason)
                return reason
        return None

    # --- pre-order gate -----------------------------------------------------

    def check_order(
        self,
        cost_usd: float,
        city: str | None,
        bankroll: float,
        opened_this_cycle: int,
    ) -> str | None:
        """Run IMMEDIATELY before routing an order, against live DB state.
        Returns a rejection reason, or None when the order may proceed."""
        lim = self.limits

        halted, halt_reason = db.is_halted(self.conn)
        if halted:
            return f"halted:{halt_reason}"

        if opened_this_cycle >= lim.max_positions_per_cycle:
            return f"max_positions_per_cycle:{lim.max_positions_per_cycle}"

        if cost_usd > bankroll * lim.max_position_frac + 1e-9:
            return (
                f"max_position_size: ${cost_usd:.2f} > "
                f"{lim.max_position_frac:.0%} of ${bankroll:.2f}"
            )

        open_positions = db.get_open_positions(self.conn)
        if len(open_positions) >= lim.max_open_positions:
            return f"max_open_positions:{lim.max_open_positions}"

        total_exposure = sum(p["cost_usd"] for p in open_positions)
        if total_exposure + cost_usd > bankroll * lim.max_total_exposure_frac + 1e-9:
            return (
                f"max_total_exposure: ${total_exposure + cost_usd:.2f} > "
                f"{lim.max_total_exposure_frac:.0%} of ${bankroll:.2f}"
            )

        if city:
            city_exposure = sum(
                p["cost_usd"] for p in open_positions if (p["city"] or "") == city
            )
            if city_exposure + cost_usd > bankroll * lim.max_city_exposure_frac + 1e-9:
                return (
                    f"max_city_exposure:{city}: ${city_exposure + cost_usd:.2f} > "
                    f"{lim.max_city_exposure_frac:.0%} of ${bankroll:.2f}"
                )

        return None
