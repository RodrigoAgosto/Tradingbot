"""Gamma response validation, ensemble reduction, and halted-cycle behavior."""

from weatherbot import db
from weatherbot.config import Settings
from weatherbot.cycle import run_cycle
from weatherbot.forecast.openmeteo import _reduce_daily
from weatherbot.markets.gamma import GammaMarket, looks_like_weather


def test_gamma_json_encoded_fields():
    m = GammaMarket.model_validate({
        "id": "123",
        "question": "Highest temperature in NYC on August 27?",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["111", "222"]',
        "volume24hr": "6000.5",
        "endDate": "2026-08-28T12:00:00Z",
    })
    assert m.outcomes == ["Yes", "No"]
    assert m.yes_token_id() == "111"
    assert m.no_token_id() == "222"
    assert m.volume_24h == 6000.5
    assert looks_like_weather(m)


def test_gamma_non_weather_filtered():
    m = GammaMarket.model_validate({"id": "1", "question": "Will the Fed cut rates?"})
    assert not looks_like_weather(m)


def test_reduce_daily_pools_members():
    times = [f"2026-08-27T{h:02d}:00" for h in range(24)]
    payload = {
        "hourly": {
            "time": times,
            "temperature_2m": [70 + (h % 12) for h in range(24)],          # control
            "temperature_2m_member01": [71 + (h % 12) for h in range(24)],  # member
        }
    }
    daily = _reduce_daily([payload])
    assert "2026-08-27" in daily
    assert daily["2026-08-27"]["high"] == [81, 82]
    assert daily["2026-08-27"]["low"] == [70, 71]


def test_reduce_daily_remaining_hours_for_today():
    times = [f"2026-08-27T{h:02d}:00" for h in range(24)]
    # peaks at midday (h=12 -> 82), evening cools to 74
    series = [70 + h for h in range(13)] + [82 - (h - 12) for h in range(13, 24)]
    payload = {"hourly": {"time": times, "temperature_2m": series}}
    full = _reduce_daily([payload])
    assert full["2026-08-27"]["high"] == [82]
    # at 17:00 local the midday peak is history; remaining max is hour 17 (77)
    remaining = _reduce_daily([payload], today="2026-08-27", from_hour=17)
    assert remaining["2026-08-27"]["high"] == [77]


def test_nws_hourly_metar_selection():
    from weatherbot.forecast import nws

    def feat(ts, value, qc="V"):
        return {"properties": {"timestamp": ts, "temperature": {"value": value, "qualityControl": qc}}}

    features = [
        # 5-minute whole-degree obs claiming 26C (78.8F raw conversion)...
        feat("2026-08-27T15:45:00+00:00", 26),
        feat("2026-08-27T15:50:00+00:00", 26),
        feat("2026-08-27T15:55:00+00:00", 26),
        # ...but the :51 hourly METAR (tenths precision) reads 25.0C = 77F
        feat("2026-08-27T15:51:00+00:00", 25.0),
        # early-minute 5-min obs are ignored entirely
        feat("2026-08-27T15:10:00+00:00", 27),
        # bad-QC readings are dropped
        feat("2026-08-27T16:51:00+00:00", 40, qc="X"),
    ]
    temps_f, last_obs, current_f = nws.select_hourly_temps(features)
    assert temps_f == [77.0]  # the METAR wins over the surrounding 5-min obs
    assert last_obs is not None
    assert current_f == 77.0
    assert nws._c_to_display_f(25.6) == 78.0  # rounds like the display


def test_halted_cycle_does_nothing(tmp_path):
    db_path = tmp_path / "halted.db"
    conn = db.connect(db_path)
    db.set_halt(conn, "unit test halt")
    conn.close()

    settings = Settings(mode="paper", db_path=str(db_path))
    report = run_cycle(settings)  # must exit before any network call
    assert report.status == "halted"

    conn = db.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM cycles").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    conn.close()
