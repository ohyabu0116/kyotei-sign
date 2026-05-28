"""
過去データ収集

使い方:
  python3 collect.py backfill 20240101 20260526   # 期間指定
  python3 collect.py day      20260526            # 1日分

挙動:
- 指定範囲内の各日について、まず日次一覧を取得
- 女子戦の会場のみピックアップ
- 各レース(1〜12R)の出走表と結果を取得して DB に保存
- スクレイピング間隔は scraper.REQUEST_INTERVAL (デフォ 1.0秒)
- 既に DB にあるレースはスキップ（再収集したいときは --force）
"""
from __future__ import annotations

import sys
import time
import datetime
from typing import Callable, Optional

import scraper
import db


def daterange(start: str, end: str, reverse: bool = False):
    s = datetime.datetime.strptime(start, "%Y%m%d").date()
    e = datetime.datetime.strptime(end, "%Y%m%d").date()
    days = []
    cur = s
    while cur <= e:
        days.append(cur.strftime("%Y%m%d"))
        cur += datetime.timedelta(days=1)
    if reverse:
        days.reverse()  # 新しい日付から収集（現役選手データを先に貯める）
    for d in days:
        yield d


def has_card_in_db(conn, date: str, jcd: str, rno: int) -> bool:
    row = conn.execute(
        "SELECT has_card, has_result FROM races WHERE date=? AND jcd=? AND rno=?",
        (date, jcd, rno),
    ).fetchone()
    return bool(row and row["has_card"]) if row else False


def has_result_in_db(conn, date: str, jcd: str, rno: int) -> bool:
    row = conn.execute(
        "SELECT has_result FROM races WHERE date=? AND jcd=? AND rno=?",
        (date, jcd, rno),
    ).fetchone()
    return bool(row and row["has_result"]) if row else False


def _retry(fn, *, tries: int = 3, wait: float = 5.0):
    """ネットワークエラー時にリトライ。全部失敗したら最後の例外を投げる。"""
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            last = e
            time.sleep(wait)
    raise last


def collect_day(date: str, *, force: bool = False,
                progress: Optional[Callable[[str], None]] = None) -> dict:
    """1日分の女子戦レースを収集"""
    progress = progress or (lambda s: print(s, flush=True))
    progress(f"[{date}] index取得...")
    # index取得はリトライ付き（タイムアウトで全体が死なないように）
    venues = _retry(lambda: scraper.fetch_daily_venues(date))
    ladies_venues = [v for v in venues if v["is_ladies"]]
    progress(f"[{date}] 女子戦会場: {len(ladies_venues)} / 全会場 {len(venues)}")

    stats = {"date": date, "ladies_venues": len(ladies_venues),
             "cards": 0, "results": 0, "errors": 0, "skipped": 0}

    db.init_db()
    with db.get_conn() as conn:
        for v in ladies_venues:
            jcd = v["jcd"]
            for rno in range(1, 13):
                # 出走表
                if not force and has_card_in_db(conn, date, jcd, rno):
                    stats["skipped"] += 1
                else:
                    try:
                        card = scraper.fetch_racecard(date, jcd, rno)
                        if card.entries:
                            db.save_racecard(conn, card)
                            stats["cards"] += 1
                            progress(f"  [{date} {v['venue']} {rno}R] card OK ({len(card.entries)}艇)")
                        else:
                            progress(f"  [{date} {v['venue']} {rno}R] card 空（未開催?）")
                    except Exception as e:
                        stats["errors"] += 1
                        progress(f"  [{date} {v['venue']} {rno}R] card ERR: {e}")

                # 結果
                if not force and has_result_in_db(conn, date, jcd, rno):
                    continue
                try:
                    result = scraper.fetch_result(date, jcd, rno)
                    if result and result.finish:
                        db.save_result(conn, result)
                        stats["results"] += 1
                        progress(f"  [{date} {v['venue']} {rno}R] result OK ({result.payout_3t}=¥{result.payout_3t_yen})")
                except Exception as e:
                    stats["errors"] += 1
                    progress(f"  [{date} {v['venue']} {rno}R] result ERR: {e}")
            # この会場は女子戦なので、収集した全レースを is_ladies=1 に確定
            # （タイトルがＶＳ等の略称でキーワード判定を漏れても確実に女子戦扱い）
            conn.execute(
                "UPDATE races SET is_ladies=1 WHERE date=? AND jcd=?",
                (date, jcd),
            )
        conn.commit()
    return stats


def collect_range(start: str, end: str, *, force: bool = False, reverse: bool = False,
                  progress: Optional[Callable[[str], None]] = None) -> dict:
    """期間内の女子戦データを収集"""
    progress = progress or (lambda s: print(s, flush=True))
    grand: dict = {"days": 0, "cards": 0, "results": 0, "errors": 0, "skipped": 0,
                   "started_at": datetime.datetime.now().isoformat()}
    for d in daterange(start, end, reverse=reverse):
        try:
            s = collect_day(d, force=force, progress=progress)
            grand["days"] += 1
            for k in ("cards", "results", "errors", "skipped"):
                grand[k] += s[k]
        except Exception as e:
            # 1日分が失敗しても全体は止めない（次の日へ）
            grand["errors"] += 1
            progress(f"[{d}] 日次収集エラー（スキップ）: {e}")
    grand["finished_at"] = datetime.datetime.now().isoformat()
    return grand


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "day":
        date = sys.argv[2]
        print(collect_day(date))
    elif cmd == "backfill":
        start, end = sys.argv[2], sys.argv[3]
        force = "--force" in sys.argv
        reverse = "--reverse" in sys.argv
        print(collect_range(start, end, force=force, reverse=reverse))
    else:
        print(__doc__)
        sys.exit(1)
