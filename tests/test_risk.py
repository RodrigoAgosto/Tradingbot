"""Hard risk-cap enforcement and halt persistence."""

from weatherbot import db
from weatherbot.config import RiskConfig
from weatherbot.risk import EffectiveLimits, RiskManager


def _open_position(conn, market_id, cost, city="New York"):
    db.open_position(conn, {
        "market_id": market_id, "token_id": f"t{market_id}", "city": city,
        "station_id": "KLGA", "side": "YES", "shares": cost * 2,
        "avg_price": 0.5, "cost_usd": cost,
    })


def test_config_cannot_loosen_code_ceilings():
    loose = RiskConfig(
        max_position_frac=0.5, max_total_exposure_frac=0.9, max_open_positions=100,
        max_city_exposure_frac=0.8, max_positions_per_cycle=10,
        daily_loss_frac=0.9, bankroll_floor_usd=1.0,
    )
    lim = EffectiveLimits.from_config(loose)
    assert lim.max_position_frac == 0.05
    assert lim.max_total_exposure_frac == 0.40
    assert lim.max_open_positions == 8
    assert lim.max_city_exposure_frac == 0.15
    assert lim.max_positions_per_cycle == 2
    assert lim.daily_loss_frac == 0.15
    assert lim.bankroll_floor_usd == 25.0


def test_config_can_tighten():
    tight = RiskConfig(max_position_frac=0.02, bankroll_floor_usd=100.0)
    lim = EffectiveLimits.from_config(tight)
    assert lim.max_position_frac == 0.02
    assert lim.bankroll_floor_usd == 100.0


def test_max_position_size(conn):
    rm = RiskManager(conn, RiskConfig())
    assert rm.check_order(51.0, "New York", 1000.0, 0) is not None  # > 5%
    assert rm.check_order(49.0, "New York", 1000.0, 0) is None


def test_max_open_positions(conn):
    rm = RiskManager(conn, RiskConfig())
    for i in range(8):
        _open_position(conn, f"m{i}", 1.0)
    assert "max_open_positions" in rm.check_order(5.0, "New York", 1000.0, 0)


def test_max_total_exposure(conn):
    rm = RiskManager(conn, RiskConfig())
    for i, cost in enumerate([50.0] * 7):
        _open_position(conn, f"m{i}", cost, city=f"c{i}")
    # 350 open + 49 order < 400 cap ok; push over:
    assert rm.check_order(49.0, "cX", 1000.0, 0) is None
    _open_position(conn, "m8", 50.0, city="c8")
    reason = rm.check_order(20.0, "cX", 1000.0, 0)
    assert reason is not None  # 8 open positions now -> count cap also fires
    assert "max" in reason


def test_max_city_exposure(conn):
    rm = RiskManager(conn, RiskConfig())
    _open_position(conn, "m1", 100.0, city="New York")
    _open_position(conn, "m2", 45.0, city="New York")
    reason = rm.check_order(10.0, "New York", 1000.0, 0)
    assert reason is not None and "max_city_exposure" in reason
    assert rm.check_order(10.0, "Chicago", 1000.0, 0) is None


def test_max_positions_per_cycle(conn):
    rm = RiskManager(conn, RiskConfig())
    assert rm.check_order(10.0, "New York", 1000.0, 2) is not None
    assert rm.check_order(10.0, "New York", 1000.0, 1) is None


def test_daily_loss_kill_switch_halts_and_persists(conn, tmp_path):
    rm = RiskManager(conn, RiskConfig())
    reason = rm.check_kill_switches(bankroll=840.0, day_start_bankroll=1000.0)
    assert reason is not None and "daily_loss" in reason
    halted, r = db.is_halted(conn)
    assert halted and "daily_loss" in r
    # persists across a "restart" (fresh connection to the same file)
    path = conn.execute("PRAGMA database_list").fetchone()["file"]
    conn2 = db.connect(path)
    halted2, _ = db.is_halted(conn2)
    assert halted2
    # only the operator clears it
    db.clear_halt(conn2)
    assert db.is_halted(conn2) == (False, None)
    conn2.close()


def test_bankroll_floor_halts(conn):
    rm = RiskManager(conn, RiskConfig())
    reason = rm.check_kill_switches(bankroll=24.0, day_start_bankroll=24.0)
    assert reason is not None and "bankroll_floor" in reason
    assert db.is_halted(conn)[0]


def test_orders_rejected_while_halted(conn):
    rm = RiskManager(conn, RiskConfig())
    db.set_halt(conn, "manual test")
    reason = rm.check_order(10.0, "New York", 1000.0, 0)
    assert reason is not None and reason.startswith("halted")
