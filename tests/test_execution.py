"""Paper executor behavior and live-mode guardrails."""

import pytest

from weatherbot import db
from weatherbot.config import Settings
from weatherbot.execution.paper import PaperExecutor
from weatherbot.execution.router import get_executor
from weatherbot.execution.types import CloseIntent, OrderIntent


def intent(cost=50.0, price=0.5):
    return OrderIntent(
        market_id="m1", token_id="tok1", side="YES", price=price,
        shares=cost / price, cost_usd=cost, city="New York", station_id="KLGA",
        claim_json="{}", resolution_date="2026-08-27",
    )


def test_paper_open_debits_bankroll(conn):
    ex = PaperExecutor(conn, starting_bankroll=1000.0)
    report = ex.open(intent(50.0), cycle_id=None)
    assert report.ok
    assert ex.bankroll() == 950.0
    assert db.has_position(conn, "m1")
    orders = conn.execute("SELECT * FROM orders").fetchall()
    assert len(orders) == 1 and orders[0]["status"] == "filled"


def test_paper_open_rejects_over_bankroll(conn):
    ex = PaperExecutor(conn, starting_bankroll=10.0)
    report = ex.open(intent(50.0), cycle_id=None)
    assert not report.ok
    assert not db.has_position(conn, "m1")


def test_paper_close_realizes_pnl(conn):
    ex = PaperExecutor(conn, 1000.0)
    ex.open(intent(50.0, price=0.5), None)  # 100 shares
    ex.close(CloseIntent("m1", "tok1", "YES", price=0.6, shares=100.0,
                         cost_basis_usd=50.0), None)
    assert abs(ex.bankroll() - 1010.0) < 1e-9  # -50 +60
    pos = conn.execute("SELECT * FROM positions WHERE market_id='m1'").fetchone()
    assert pos["status"] == "closed" and abs(pos["pnl_usd"] - 10.0) < 1e-9


def test_paper_settlement(conn):
    ex = PaperExecutor(conn, 1000.0)
    ex.open(intent(50.0, price=0.5), None)
    pnl = ex.settle("m1", won=True, shares=100.0, cost_basis_usd=50.0)
    assert abs(pnl - 50.0) < 1e-9
    assert abs(ex.bankroll() - 1050.0) < 1e-9
    pos = conn.execute("SELECT * FROM positions WHERE market_id='m1'").fetchone()
    assert pos["status"] == "resolved" and pos["outcome"] == "won"


def test_router_defaults_to_paper(conn):
    ex = get_executor(conn, Settings(mode="paper"), live_ack=False)
    assert ex.mode == "paper"


def test_router_paper_even_with_ack_flag(conn):
    # flag alone must not enable live
    ex = get_executor(conn, Settings(mode="paper"), live_ack=True)
    assert ex.mode == "paper"


def test_live_refused_without_ack(conn, monkeypatch):
    from weatherbot.execution.live import LiveTradingRefused

    with pytest.raises(LiveTradingRefused):
        get_executor(conn, Settings(mode="live"), live_ack=False)


def test_live_refused_without_client_or_key(conn, monkeypatch):
    from weatherbot.execution.live import LiveTradingRefused

    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    # even with ack + live mode, missing py-clob-client and/or key refuses
    with pytest.raises(LiveTradingRefused):
        get_executor(conn, Settings(mode="live"), live_ack=True)


def test_key_file_permissions_enforced_posix(monkeypatch, tmp_path):
    import os

    import pytest as _pytest

    if os.name == "nt":
        _pytest.skip("POSIX permission bits are not meaningful on Windows")

    from weatherbot.config import KeyFileError, load_private_key

    key_file = tmp_path / "key.env"
    key_file.write_text("x")
    key_file.chmod(0o644)  # group/other readable -> refused
    monkeypatch.setenv("POLYMARKET_KEY_FILE", str(key_file))
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "deadbeef" * 8)
    with pytest.raises(KeyFileError, match="must be 600"):
        load_private_key()

    key_file.chmod(0o600)
    assert load_private_key() == "deadbeef" * 8


def test_key_env_file_loaded_without_overriding(monkeypatch, tmp_path):
    from weatherbot.config import load_key_env_file

    key_file = tmp_path / "key.env"
    key_file.write_text(
        "# comment\n"
        "TELEGRAM_BOT_TOKEN=from-file\n"
        "TRADING_MODE=live\n"
        "EMPTY_VALUE=\n"
    )
    monkeypatch.setenv("WEATHERBOT_KEY_FILE", str(key_file))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TRADING_MODE", "paper")  # pre-set env must win
    assert load_key_env_file() == key_file
    import os

    assert os.environ["TELEGRAM_BOT_TOKEN"] == "from-file"
    assert os.environ["TRADING_MODE"] == "paper"
    assert "EMPTY_VALUE" not in os.environ


def test_trading_mode_env_fail_closed(monkeypatch, tmp_path):
    from weatherbot.config import load_settings

    monkeypatch.setenv("TRADING_MODE", "LIVE!!")  # anything not exactly 'live'
    s = load_settings(tmp_path / "nonexistent.yaml")
    assert s.mode == "paper"


def test_live_preflight_fails_closed_without_client(monkeypatch, capsys):
    # py-clob-client is not installed in the test environment, so preflight
    # must stop at step 1 with the fix printed — and never touch the network
    from weatherbot.execution.live import live_preflight

    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    rc = live_preflight()
    out = capsys.readouterr().out
    assert rc == 1
    assert "no orders will be placed" in out
    assert "uv sync --extra live" in out
