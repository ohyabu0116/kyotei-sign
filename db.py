"""SQLite 永続層"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict
from typing import Iterable, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "kyotei_sign.db")
_lock = threading.Lock()


SCHEMA = """
CREATE TABLE IF NOT EXISTS races (
    date     TEXT NOT NULL,        -- YYYYMMDD
    jcd      TEXT NOT NULL,        -- 会場コード
    rno      INTEGER NOT NULL,     -- レース番号
    venue    TEXT,
    title    TEXT,
    is_ladies INTEGER NOT NULL DEFAULT 0,
    has_card INTEGER NOT NULL DEFAULT 0,
    has_result INTEGER NOT NULL DEFAULT 0,
    fetched_at TEXT,
    PRIMARY KEY (date, jcd, rno)
);

CREATE INDEX IF NOT EXISTS idx_races_ladies ON races(is_ladies, date);
CREATE INDEX IF NOT EXISTS idx_races_date ON races(date);

CREATE TABLE IF NOT EXISTS race_entries (
    date     TEXT NOT NULL,
    jcd      TEXT NOT NULL,
    rno      INTEGER NOT NULL,
    lane     INTEGER NOT NULL,
    toban    TEXT NOT NULL,
    name     TEXT,
    rank     TEXT,
    branch   TEXT,
    home     TEXT,
    age      INTEGER,
    weight   REAL,
    flying   INTEGER,
    late     INTEGER,
    avg_st   REAL,
    national_win REAL,
    national_2 REAL,
    national_3 REAL,
    local_win REAL,
    local_2 REAL,
    local_3 REAL,
    motor_no INTEGER,
    motor_2 REAL,
    motor_3 REAL,
    boat_no INTEGER,
    boat_2 REAL,
    boat_3 REAL,
    PRIMARY KEY (date, jcd, rno, lane),
    FOREIGN KEY (date, jcd, rno) REFERENCES races(date, jcd, rno)
);

CREATE INDEX IF NOT EXISTS idx_entries_toban ON race_entries(toban);

CREATE TABLE IF NOT EXISTS race_results (
    date     TEXT NOT NULL,
    jcd      TEXT NOT NULL,
    rno      INTEGER NOT NULL,
    finish_json TEXT,            -- [[rank, lane, toban, name], ...]
    determinant TEXT,
    payout_3t TEXT,
    payout_3t_yen INTEGER,
    payout_3f TEXT,
    payout_3f_yen INTEGER,
    wind_dir TEXT,
    wind_speed INTEGER,
    wave INTEGER,
    weather TEXT,
    PRIMARY KEY (date, jcd, rno),
    FOREIGN KEY (date, jcd, rno) REFERENCES races(date, jcd, rno)
);

CREATE TABLE IF NOT EXISTS signs (
    sign_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    toban       TEXT NOT NULL,
    racer_name  TEXT,
    cond_lane   INTEGER NOT NULL,   -- 条件部: 何号艇に乗ったとき
    target_pair TEXT NOT NULL,      -- 結果部: "M-P" (M<P) 両方3着以内
    target_kind TEXT NOT NULL,      -- 'top3_pair' or 'win'
    support     INTEGER NOT NULL,   -- 発火回数
    hits        INTEGER NOT NULL,   -- 的中回数
    confidence  REAL NOT NULL,      -- hits / support
    lift        REAL,
    p_value     REAL,
    q_value     REAL,               -- 多重比較補正後のFDR q値 (Benjamini-Hochberg)
    last_seen   TEXT,
    discovered_at TEXT,
    UNIQUE(toban, cond_lane, target_pair, target_kind)
);

CREATE INDEX IF NOT EXISTS idx_signs_conf ON signs(confidence DESC, support DESC);
CREATE INDEX IF NOT EXISTS idx_signs_toban ON signs(toban, cond_lane);

CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT,
    period_from TEXT,
    period_to   TEXT,
    sign_count  INTEGER,
    races_count INTEGER,
    fires_count INTEGER,
    hits_count  INTEGER,
    payout_sum  INTEGER,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS scrape_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT,
    kind        TEXT,
    target      TEXT,
    status      TEXT,
    note        TEXT
);
"""


def init_db(db_path: str = DB_PATH) -> None:
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """冪等マイグレーション: 既存DBに不足している列を追加する（既存データは壊さない）。"""
    # signs.q_value (多重比較補正後のFDR q値)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(signs)")}
    if "q_value" not in cols:
        conn.execute("ALTER TABLE signs ADD COLUMN q_value REAL")


@contextmanager
def get_conn(db_path: str = DB_PATH):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ── 書き込み ─────────────────────────────────────────────────────
def upsert_race(conn, date: str, jcd: str, rno: int, venue: str, title: str,
                is_ladies: bool, has_card: bool = False, has_result: bool = False) -> None:
    import datetime
    conn.execute("""
        INSERT INTO races(date, jcd, rno, venue, title, is_ladies, has_card, has_result, fetched_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date, jcd, rno) DO UPDATE SET
            venue=excluded.venue,
            title=excluded.title,
            is_ladies=excluded.is_ladies,
            has_card=races.has_card OR excluded.has_card,
            has_result=races.has_result OR excluded.has_result,
            fetched_at=excluded.fetched_at
    """, (date, jcd, rno, venue, title, int(is_ladies),
          int(has_card), int(has_result),
          datetime.datetime.now().isoformat()))


def save_racecard(conn, card) -> None:
    """RaceCard を races + race_entries に書き込む"""
    upsert_race(conn, card.date, card.jcd, card.rno, card.venue, card.title,
                card.is_ladies, has_card=True)
    # 既存エントリを消して再投入
    conn.execute(
        "DELETE FROM race_entries WHERE date=? AND jcd=? AND rno=?",
        (card.date, card.jcd, card.rno),
    )
    for e in card.entries:
        conn.execute("""
            INSERT INTO race_entries(date, jcd, rno, lane, toban, name, rank, branch, home,
                age, weight, flying, late, avg_st,
                national_win, national_2, national_3,
                local_win, local_2, local_3,
                motor_no, motor_2, motor_3, boat_no, boat_2, boat_3)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (card.date, card.jcd, card.rno, e.lane, e.toban, e.name, e.rank,
              e.branch, e.home, e.age, e.weight, e.flying, e.late, e.avg_st,
              e.national_win, e.national_2, e.national_3,
              e.local_win, e.local_2, e.local_3,
              e.motor_no, e.motor_2, e.motor_3,
              e.boat_no, e.boat_2, e.boat_3))


def save_result(conn, result) -> None:
    """RaceResult を races + race_results に書き込む"""
    upsert_race(conn, result.date, result.jcd, result.rno, result.venue, result.title,
                any(k in result.title for k in
                    ("ヴィーナス", "オールレディース", "レディース", "クイーン")),
                has_result=True)
    conn.execute("""
        INSERT INTO race_results(date, jcd, rno, finish_json, determinant,
            payout_3t, payout_3t_yen, payout_3f, payout_3f_yen,
            wind_dir, wind_speed, wave, weather)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date, jcd, rno) DO UPDATE SET
            finish_json=excluded.finish_json,
            determinant=excluded.determinant,
            payout_3t=excluded.payout_3t,
            payout_3t_yen=excluded.payout_3t_yen,
            payout_3f=excluded.payout_3f,
            payout_3f_yen=excluded.payout_3f_yen,
            wind_dir=excluded.wind_dir,
            wind_speed=excluded.wind_speed,
            wave=excluded.wave,
            weather=excluded.weather
    """, (result.date, result.jcd, result.rno,
          json.dumps(result.finish, ensure_ascii=False),
          result.determinant,
          result.payout_3t, result.payout_3t_yen,
          result.payout_3f, result.payout_3f_yen,
          result.wind_dir, result.wind_speed, result.wave, result.weather))


# ── 統計 ─────────────────────────────────────────────────────────
def db_stats(conn) -> dict:
    cur = conn.cursor()
    out: dict = {}
    for tbl in ("races", "race_entries", "race_results", "signs"):
        cur.execute(f"SELECT COUNT(*) FROM {tbl}")
        out[tbl] = cur.fetchone()[0]
    cur.execute("SELECT MIN(date), MAX(date) FROM races")
    mn, mx = cur.fetchone()
    out["date_range"] = [mn, mx]
    cur.execute("SELECT COUNT(*) FROM races WHERE is_ladies=1")
    out["ladies_races"] = cur.fetchone()[0]
    return out


if __name__ == "__main__":
    init_db()
    with get_conn() as conn:
        print(db_stats(conn))
