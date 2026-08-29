"""SQLite schema, migrations and all queries.

Plain sqlite3, no ORM. Every table is inspectable with the sqlite3 CLI,
which is what the nightly review layer uses (read-only).

Times are stored as ISO-8601 UTC strings. Dates as YYYY-MM-DD.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 2

MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE cycles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        mode TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'running',  -- running|ok|degraded|halted|error
        bankroll REAL,
        positions_open INTEGER,
        orders_placed INTEGER DEFAULT 0,
        note TEXT
    );

    CREATE TABLE evaluations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cycle_id INTEGER NOT NULL REFERENCES cycles(id),
        market_id TEXT NOT NULL,
        question TEXT,
        claim_json TEXT,
        fair_prob REAL,
        confidence REAL,
        market_price REAL,      -- implied prob from best executable quote (YES)
        exec_price REAL,        -- avg fill price for intended size, chosen side
        side TEXT,              -- YES|NO
        edge REAL,
        lead_days REAL,
        volume_24h REAL,
        decision TEXT NOT NULL, -- enter|exit|hold|skip
        skip_reason TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cycle_id INTEGER REFERENCES cycles(id),
        market_id TEXT NOT NULL,
        token_id TEXT,
        side TEXT NOT NULL,       -- YES|NO (token bought)
        action TEXT NOT NULL,     -- open|close
        price REAL NOT NULL,
        shares REAL NOT NULL,
        cost_usd REAL NOT NULL,
        mode TEXT NOT NULL,       -- paper|live
        status TEXT NOT NULL,     -- intended|filled|rejected|error
        detail TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        market_id TEXT NOT NULL UNIQUE,
        token_id TEXT,
        city TEXT,
        station_id TEXT,
        side TEXT NOT NULL,
        shares REAL NOT NULL,
        avg_price REAL NOT NULL,
        cost_usd REAL NOT NULL,
        claim_json TEXT,
        resolution_date TEXT,
        opened_at TEXT NOT NULL,
        closed_at TEXT,
        status TEXT NOT NULL DEFAULT 'open',  -- open|closed|resolved
        outcome TEXT,                          -- won|lost|exited
        pnl_usd REAL
    );

    CREATE TABLE paper_account (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        bankroll REAL NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE daily_bankroll (
        day TEXT PRIMARY KEY,
        starting_bankroll REAL NOT NULL
    );

    CREATE TABLE halt (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        halted INTEGER NOT NULL DEFAULT 0,
        reason TEXT,
        halted_at TEXT,
        cleared_at TEXT
    );

    CREATE TABLE forecast_cache (
        cache_key TEXT PRIMARY KEY,
        station_id TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        payload TEXT NOT NULL
    );

    CREATE TABLE observations (
        station_id TEXT NOT NULL,
        day TEXT NOT NULL,
        high_f REAL,
        low_f REAL,
        last_obs_at TEXT,
        final INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (station_id, day)
    );

    CREATE TABLE calibration_obs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        station_id TEXT NOT NULL,
        metric TEXT NOT NULL,
        lead_bucket INTEGER NOT NULL,   -- whole days of lead time
        target_day TEXT NOT NULL,
        forecast_mean REAL NOT NULL,
        forecast_std REAL NOT NULL,
        actual REAL,                    -- filled in once observed
        created_at TEXT NOT NULL,
        UNIQUE (station_id, metric, lead_bucket, target_day)
    );

    CREATE TABLE meta (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    """,
    2: """
    ALTER TABLE positions ADD COLUMN venue TEXT NOT NULL DEFAULT 'polymarket';
    ALTER TABLE orders ADD COLUMN venue TEXT NOT NULL DEFAULT 'polymarket';
    ALTER TABLE evaluations ADD COLUMN venue TEXT NOT NULL DEFAULT 'polymarket';
    """,
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    cur = conn.execute("PRAGMA user_version")
    version = cur.fetchone()[0]
    for target in sorted(MIGRATIONS):
        if target > version:
            conn.executescript(MIGRATIONS[target])
            conn.execute(f"PRAGMA user_version = {target}")
    conn.execute("INSERT OR IGNORE INTO halt (id, halted) VALUES (1, 0)")
    conn.commit()


# --- halt / kill switch ----------------------------------------------------

def is_halted(conn: sqlite3.Connection) -> tuple[bool, str | None]:
    row = conn.execute("SELECT halted, reason FROM halt WHERE id = 1").fetchone()
    if row is None:
        return False, None
    return bool(row["halted"]), row["reason"]


def set_halt(conn: sqlite3.Connection, reason: str) -> None:
    conn.execute(
        "UPDATE halt SET halted = 1, reason = ?, halted_at = ?, cleared_at = NULL WHERE id = 1",
        (reason, utcnow()),
    )
    conn.commit()


def clear_halt(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE halt SET halted = 0, reason = NULL, cleared_at = ? WHERE id = 1",
        (utcnow(),),
    )
    conn.commit()


# --- paper account ---------------------------------------------------------

def get_paper_bankroll(conn: sqlite3.Connection, starting: float) -> float:
    row = conn.execute("SELECT bankroll FROM paper_account WHERE id = 1").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO paper_account (id, bankroll, updated_at) VALUES (1, ?, ?)",
            (starting, utcnow()),
        )
        conn.commit()
        return starting
    return float(row["bankroll"])


def set_paper_bankroll(conn: sqlite3.Connection, bankroll: float) -> None:
    conn.execute(
        "INSERT INTO paper_account (id, bankroll, updated_at) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET bankroll = excluded.bankroll, updated_at = excluded.updated_at",
        (bankroll, utcnow()),
    )
    conn.commit()


# --- daily bankroll snapshot (for the daily-loss kill switch) --------------

def get_day_start_bankroll(conn: sqlite3.Connection, day: date, bankroll_now: float) -> float:
    key = day.isoformat()
    row = conn.execute(
        "SELECT starting_bankroll FROM daily_bankroll WHERE day = ?", (key,)
    ).fetchone()
    if row is not None:
        return float(row["starting_bankroll"])
    conn.execute(
        "INSERT INTO daily_bankroll (day, starting_bankroll) VALUES (?, ?)",
        (key, bankroll_now),
    )
    conn.commit()
    return bankroll_now


# --- cycles ----------------------------------------------------------------

def start_cycle(conn: sqlite3.Connection, mode: str) -> int:
    cur = conn.execute(
        "INSERT INTO cycles (started_at, mode) VALUES (?, ?)", (utcnow(), mode)
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_cycle(
    conn: sqlite3.Connection,
    cycle_id: int,
    status: str,
    bankroll: float | None = None,
    positions_open: int | None = None,
    orders_placed: int = 0,
    note: str | None = None,
) -> None:
    conn.execute(
        "UPDATE cycles SET finished_at = ?, status = ?, bankroll = ?, "
        "positions_open = ?, orders_placed = ?, note = ? WHERE id = ?",
        (utcnow(), status, bankroll, positions_open, orders_placed, note, cycle_id),
    )
    conn.commit()


# --- evaluations -----------------------------------------------------------

def record_evaluation(conn: sqlite3.Connection, cycle_id: int, ev: dict) -> None:
    conn.execute(
        """INSERT INTO evaluations
           (cycle_id, market_id, question, claim_json, fair_prob, confidence,
            market_price, exec_price, side, edge, lead_days, volume_24h,
            decision, skip_reason, created_at, venue)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            cycle_id,
            ev.get("market_id"),
            ev.get("question"),
            json.dumps(ev["claim"]) if ev.get("claim") else None,
            ev.get("fair_prob"),
            ev.get("confidence"),
            ev.get("market_price"),
            ev.get("exec_price"),
            ev.get("side"),
            ev.get("edge"),
            ev.get("lead_days"),
            ev.get("volume_24h"),
            ev["decision"],
            ev.get("skip_reason"),
            utcnow(),
            ev.get("venue", "polymarket"),
        ),
    )
    conn.commit()


# --- orders / positions ----------------------------------------------------

def record_order(conn: sqlite3.Connection, cycle_id: int | None, order: dict) -> int:
    cur = conn.execute(
        """INSERT INTO orders
           (cycle_id, market_id, token_id, side, action, price, shares,
            cost_usd, mode, status, detail, created_at, venue)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            cycle_id,
            order["market_id"],
            order.get("token_id"),
            order["side"],
            order["action"],
            order["price"],
            order["shares"],
            order["cost_usd"],
            order["mode"],
            order["status"],
            order.get("detail"),
            utcnow(),
            order.get("venue", "polymarket"),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def open_position(conn: sqlite3.Connection, pos: dict) -> None:
    conn.execute(
        """INSERT INTO positions
           (market_id, token_id, city, station_id, side, shares, avg_price,
            cost_usd, claim_json, resolution_date, opened_at, status, venue)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
        (
            pos["market_id"],
            pos.get("token_id"),
            pos.get("city"),
            pos.get("station_id"),
            pos["side"],
            pos["shares"],
            pos["avg_price"],
            pos["cost_usd"],
            pos.get("claim_json"),
            pos.get("resolution_date"),
            utcnow(),
            pos.get("venue", "polymarket"),
        ),
    )
    conn.commit()


def close_position(
    conn: sqlite3.Connection,
    market_id: str,
    outcome: str,
    pnl_usd: float,
    status: str = "closed",
) -> None:
    conn.execute(
        "UPDATE positions SET status = ?, outcome = ?, pnl_usd = ?, closed_at = ? "
        "WHERE market_id = ? AND status = 'open'",
        (status, outcome, pnl_usd, utcnow(), market_id),
    )
    conn.commit()


def get_open_positions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM positions WHERE status = 'open'").fetchall()


def has_position(conn: sqlite3.Connection, market_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM positions WHERE market_id = ? AND status = 'open'", (market_id,)
    ).fetchone()
    return row is not None


# --- forecast cache --------------------------------------------------------

def cache_get(conn: sqlite3.Connection, key: str, max_age_seconds: float) -> dict | None:
    row = conn.execute(
        "SELECT fetched_at, payload FROM forecast_cache WHERE cache_key = ?", (key,)
    ).fetchone()
    if row is None:
        return None
    fetched = datetime.fromisoformat(row["fetched_at"])
    age = (datetime.now(timezone.utc) - fetched).total_seconds()
    if age > max_age_seconds:
        return None
    return {"fetched_at": row["fetched_at"], "payload": json.loads(row["payload"])}


def cache_put(conn: sqlite3.Connection, key: str, station_id: str, payload: dict) -> None:
    conn.execute(
        "INSERT INTO forecast_cache (cache_key, station_id, fetched_at, payload) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(cache_key) DO UPDATE SET fetched_at = excluded.fetched_at, payload = excluded.payload",
        (key, station_id, utcnow(), json.dumps(payload)),
    )
    conn.commit()


# --- observations ----------------------------------------------------------

def upsert_observation(
    conn: sqlite3.Connection,
    station_id: str,
    day: str,
    high_f: float | None,
    low_f: float | None,
    last_obs_at: str | None,
    final: bool = False,
) -> None:
    conn.execute(
        """INSERT INTO observations (station_id, day, high_f, low_f, last_obs_at, final, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(station_id, day) DO UPDATE SET
             high_f = excluded.high_f, low_f = excluded.low_f,
             last_obs_at = excluded.last_obs_at, final = excluded.final,
             updated_at = excluded.updated_at""",
        (station_id, day, high_f, low_f, last_obs_at, int(final), utcnow()),
    )
    conn.commit()


def get_observation(conn: sqlite3.Connection, station_id: str, day: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM observations WHERE station_id = ? AND day = ?", (station_id, day)
    ).fetchone()


# --- calibration -----------------------------------------------------------

def record_forecast_snapshot(
    conn: sqlite3.Connection,
    station_id: str,
    metric: str,
    lead_bucket: int,
    target_day: str,
    forecast_mean: float,
    forecast_std: float,
) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO calibration_obs
           (station_id, metric, lead_bucket, target_day, forecast_mean, forecast_std, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (station_id, metric, lead_bucket, target_day, forecast_mean, forecast_std, utcnow()),
    )
    conn.commit()


def fill_calibration_actuals(conn: sqlite3.Connection) -> int:
    """Backfill `actual` from finalized observations. Returns rows updated."""
    cur = conn.execute(
        """UPDATE calibration_obs SET actual = (
             SELECT CASE calibration_obs.metric
                      WHEN 'high_temp' THEN o.high_f
                      WHEN 'low_temp' THEN o.low_f
                    END
             FROM observations o
             WHERE o.station_id = calibration_obs.station_id
               AND o.day = calibration_obs.target_day AND o.final = 1
           )
           WHERE actual IS NULL AND EXISTS (
             SELECT 1 FROM observations o
             WHERE o.station_id = calibration_obs.station_id
               AND o.day = calibration_obs.target_day AND o.final = 1
           )"""
    )
    conn.commit()
    return cur.rowcount


def get_calibration_pairs(
    conn: sqlite3.Connection, station_id: str, metric: str, lead_bucket: int
) -> list[tuple[float, float, float]]:
    rows = conn.execute(
        """SELECT forecast_mean, forecast_std, actual FROM calibration_obs
           WHERE station_id = ? AND metric = ? AND lead_bucket = ? AND actual IS NOT NULL""",
        (station_id, metric, lead_bucket),
    ).fetchall()
    return [(r["forecast_mean"], r["forecast_std"], r["actual"]) for r in rows]
