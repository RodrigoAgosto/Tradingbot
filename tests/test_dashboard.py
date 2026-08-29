"""Dashboard data endpoint math."""

from weatherbot import db
from weatherbot.dashboard import build_data


def test_build_data_equity_includes_open_cost(tmp_path):
    path = tmp_path / "dash.db"
    conn = db.connect(path)
    db.set_paper_bankroll(conn, 950.0)

    # cycle 1 finishes before any position exists
    c1 = db.start_cycle(conn, "paper")
    db.finish_cycle(conn, c1, "ok", bankroll=1000.0)
    # a position opens (cash goes to 950), cycle 2 finishes after
    db.open_position(conn, {
        "market_id": "m1", "token_id": "t", "city": "London", "station_id": "EGLC",
        "side": "NO", "shares": 100, "avg_price": 0.5, "cost_usd": 50.0,
    })
    c2 = db.start_cycle(conn, "paper")
    db.finish_cycle(conn, c2, "ok", bankroll=950.0)
    # real cycles are 20 min apart; give the rows distinct timestamps so the
    # open-interval comparison isn't a same-second tie
    conn.execute("UPDATE cycles SET finished_at = '2026-08-28T10:00:00+00:00' WHERE id = ?", (c1,))
    conn.execute("UPDATE positions SET opened_at = '2026-08-28T10:10:00+00:00'")
    conn.execute("UPDATE cycles SET finished_at = '2026-08-28T10:20:00+00:00' WHERE id = ?", (c2,))
    conn.commit()
    conn.close()

    data = build_data(path)
    eq = data["equity_series"]
    assert len(eq) == 2
    assert eq[0]["equity"] == 1000.0          # no open positions yet
    assert eq[1]["equity"] == 950.0 + 50.0    # cash + open cost
    assert data["summary"]["equity"] == 1000.0
    assert data["summary"]["open_cost"] == 50.0
    assert len(data["open_positions"]) == 1
    assert data["summary"]["halted"] is False


def test_build_data_settled_and_winrate(tmp_path):
    path = tmp_path / "dash2.db"
    conn = db.connect(path)
    db.set_paper_bankroll(conn, 1010.0)
    for i, (outcome, pnl) in enumerate([("won", 30.0), ("lost", -20.0)]):
        db.open_position(conn, {
            "market_id": f"m{i}", "token_id": "t", "city": "Tokyo", "station_id": "RJTT",
            "side": "YES", "shares": 10, "avg_price": 0.5, "cost_usd": 5.0,
        })
        db.close_position(conn, f"m{i}", outcome=outcome, pnl_usd=pnl, status="resolved")
    conn.close()

    data = build_data(path)
    s = data["summary"]
    assert s["wins"] == 1 and s["losses"] == 1
    assert s["realized_pnl"] == 10.0
    assert len(data["settled"]) == 2


def test_reset_paper_wipes_ledger_keeps_calibration(tmp_path, monkeypatch, capsys):
    from weatherbot.cli import main

    dbfile = tmp_path / "reset.db"
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f'db_path: "{dbfile}"\n')
    conn = db.connect(dbfile)
    db.set_paper_bankroll(conn, 850.0)
    c = db.start_cycle(conn, "paper")
    db.finish_cycle(conn, c, "ok", bankroll=850.0)
    db.open_position(conn, {
        "market_id": "m1", "token_id": "t", "city": "Tokyo", "station_id": "RJTT",
        "side": "YES", "shares": 10, "avg_price": 0.5, "cost_usd": 5.0,
    })
    db.record_forecast_snapshot(conn, "KNYC", "high_temp", 1, "2026-08-28", 80.0, 2.0)
    conn.close()
    monkeypatch.setenv("TRADING_MODE", "paper")

    # preview does not delete
    assert main(["--config", str(cfg), "reset-paper", "--bankroll", "200"]) == 0
    conn = db.connect(dbfile)
    assert conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 1
    conn.close()

    # --yes deletes ledger, keeps calibration, sets bankroll
    assert main(["--config", str(cfg), "reset-paper", "--bankroll", "200", "--yes"]) == 0
    conn = db.connect(dbfile)
    for t in ("positions", "orders", "evaluations", "cycles", "daily_bankroll"):
        assert conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM calibration_obs").fetchone()[0] == 1
    assert db.get_paper_bankroll(conn, 999.0) == 200.0
    conn.close()

    # refused in live mode
    monkeypatch.setenv("TRADING_MODE", "live")
    assert main(["--config", str(cfg), "reset-paper", "--yes"]) == 2
