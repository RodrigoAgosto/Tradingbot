"""Live executor: real orders on the Polymarket CLOB.

Deliberately hard to reach. Instantiating LiveExecutor requires ALL of:
  1. TRADING_MODE=live in the environment (config.mode == "live"),
  2. the --i-understand-this-is-live CLI flag on this invocation,
  3. py-clob-client installed (optional dependency: `uv sync --extra live`),
  4. POLYMARKET_PRIVATE_KEY present (600-perm key file), which is
     registered for log redaction before any client is constructed.

Anything missing raises LiveTradingRefused and the cycle fails closed.
"""

from __future__ import annotations

import logging
import os
import sqlite3

from weatherbot import db, logutil
from weatherbot.config import Settings, load_private_key
from weatherbot.execution.types import CloseIntent, ExecutionReport, OrderIntent

log = logging.getLogger(__name__)

POLYGON_CHAIN_ID = 137
CLOB_HOST = "https://clob.polymarket.com"


class LiveTradingRefused(RuntimeError):
    pass


class LiveExecutor:
    mode = "live"

    def __init__(self, conn: sqlite3.Connection, settings: Settings, live_ack: bool):
        if not settings.is_live:
            raise LiveTradingRefused("TRADING_MODE is not 'live'")
        if not live_ack:
            raise LiveTradingRefused(
                "live mode requires the --i-understand-this-is-live flag"
            )
        try:
            from py_clob_client.client import ClobClient  # noqa: PLC0415
        except ImportError as exc:
            raise LiveTradingRefused(
                "py-clob-client is not installed; run `uv sync --extra live`"
            ) from exc

        key = load_private_key()
        logutil.register_secret(key)
        funder = os.environ.get("POLYMARKET_PROXY_ADDRESS", "").strip()
        if not funder:
            raise LiveTradingRefused("POLYMARKET_PROXY_ADDRESS is not set")

        self.conn = conn
        # signature_type=1: email/proxy wallet. Adjust to 2 for browser-wallet
        # accounts (see README onboarding).
        self._client = ClobClient(
            CLOB_HOST, key=key, chain_id=POLYGON_CHAIN_ID,
            signature_type=1, funder=funder,
        )
        self._client.set_api_creds(self._client.create_or_derive_api_creds())
        log.info("live executor initialized (funder=%s...)", funder[:8])

    def bankroll(self) -> float:
        """USDC balance available for trading, in dollars."""
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType  # noqa: PLC0415

        res = self._client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        return float(res["balance"]) / 1e6

    def remote_positions(self) -> list[dict]:
        """Open positions per Polymarket's data API, for reconciliation."""
        import httpx  # noqa: PLC0415

        funder = os.environ.get("POLYMARKET_PROXY_ADDRESS", "").strip()
        resp = httpx.get(
            "https://data-api.polymarket.com/positions",
            params={"user": funder, "sizeThreshold": 0.1},
            timeout=30,
        )
        resp.raise_for_status()
        return [
            {"token_id": str(p.get("asset")), "size": p.get("size")}
            for p in resp.json()
        ]

    def open(self, intent: OrderIntent, cycle_id: int | None) -> ExecutionReport:
        return self._place(intent.token_id, "BUY", intent.price, intent.shares,
                           intent, cycle_id, action="open")

    def close(self, intent: CloseIntent, cycle_id: int | None) -> ExecutionReport:
        order_intent = OrderIntent(
            market_id=intent.market_id, token_id=intent.token_id, side=intent.side,
            price=intent.price, shares=intent.shares,
            cost_usd=intent.shares * intent.price,
        )
        report = self._place(intent.token_id, "SELL", intent.price, intent.shares,
                             order_intent, cycle_id, action="close")
        if report.ok:
            proceeds = intent.shares * (report.fill_price or intent.price)
            db.close_position(self.conn, intent.market_id, outcome="exited",
                              pnl_usd=proceeds - intent.cost_basis_usd)
        return report

    def _place(self, token_id: str | None, side: str, price: float, shares: float,
               intent: OrderIntent, cycle_id: int | None, action: str) -> ExecutionReport:
        from py_clob_client.clob_types import OrderArgs, OrderType  # noqa: PLC0415
        from py_clob_client.order_builder.constants import BUY, SELL  # noqa: PLC0415

        if not token_id:
            return ExecutionReport(ok=False, detail="missing_token_id")
        row = intent.as_order_row(self.mode, "intended")
        row["action"] = action
        order_id = db.record_order(self.conn, cycle_id, row)
        try:
            args = OrderArgs(
                token_id=token_id,
                price=round(price, 2),
                size=round(shares, 2),
                side=BUY if side == "BUY" else SELL,
            )
            signed = self._client.create_order(args)
            resp = self._client.post_order(signed, OrderType.GTC)
            ok = bool(resp.get("success"))
            status = "filled" if ok else "error"
            self.conn.execute(
                "UPDATE orders SET status = ?, detail = ? WHERE id = ?",
                (status, str(resp.get("orderID") or resp.get("errorMsg"))[:200], order_id),
            )
            self.conn.commit()
            if ok and action == "open":
                db.open_position(self.conn, {
                    "market_id": intent.market_id, "token_id": intent.token_id,
                    "city": intent.city, "station_id": intent.station_id,
                    "side": intent.side, "shares": intent.shares,
                    "avg_price": intent.price, "cost_usd": intent.cost_usd,
                    "claim_json": intent.claim_json,
                    "resolution_date": intent.resolution_date,
                })
            return ExecutionReport(ok=ok, fill_price=price if ok else None,
                                   detail=None if ok else str(resp)[:200])
        except Exception as exc:
            self.conn.execute(
                "UPDATE orders SET status = 'error', detail = ? WHERE id = ?",
                (str(exc)[:200], order_id),
            )
            self.conn.commit()
            log.exception("live order failed market=%s", intent.market_id)
            return ExecutionReport(ok=False, detail=str(exc)[:200])
