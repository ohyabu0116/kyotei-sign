#!/usr/bin/env python3
"""
競艇サインマイナー サーバー

Endpoints:
  GET  /                      index.html
  GET  /api/ping              生存確認
  GET  /api/stats             DB統計
  GET  /api/today             今日の女子戦 + 発火サイン
  GET  /api/day?date=         指定日の女子戦
  GET  /api/race?date=&jcd=&rno=   レース詳細（出走表+結果+発火サイン）
  GET  /api/signs             抽出済みサイン一覧
  GET  /api/integrity         データ整合性チェック
  POST /api/collect           {start,end} で過去データ収集（バックグラウンドjob）
  GET  /api/job/<id>          ジョブ進捗
  POST /api/mine              サインマイニング実行
  POST /api/backtest          バックテスト実行
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
import datetime
import threading
import sqlite3
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import scraper
import db
import miner
import backtest

PORT = int(os.environ.get("PORT", 8772))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(BASE_DIR, "index.html")

# ── ジョブ管理 ───────────────────────────────────────────────────
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _job_set(job_id: str, **fields) -> None:
    with _jobs_lock:
        j = _jobs.setdefault(job_id, {"id": job_id, "log": []})
        j.update(fields)


def _job_log(job_id: str, line: str) -> None:
    with _jobs_lock:
        j = _jobs.setdefault(job_id, {"id": job_id, "log": []})
        j["log"].append(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {line}")
        if len(j["log"]) > 500:
            j["log"] = j["log"][-500:]


def _job_get(job_id: str) -> dict | None:
    with _jobs_lock:
        return dict(_jobs.get(job_id, {})) if job_id in _jobs else None


# ── ヘルパー ─────────────────────────────────────────────────────
def _json_response(handler: BaseHTTPRequestHandler, status: int, data) -> None:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()
    handler.wfile.write(body)


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    if not length:
        return {}
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


# ── ハンドラ ─────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[{self.address_string()}] {format % args}", flush=True)

    # ── GET ────────────────────────────────────────────────────
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        q = parse_qs(parsed.query)

        try:
            if path == "/" or path == "/index.html":
                return self._serve_index()
            if path == "/api/ping":
                return _json_response(self, 200, {"ok": True, "ts": time.time()})
            if path == "/api/stats":
                return self._api_stats()
            if path == "/api/today":
                return self._api_today()
            if path == "/api/day":
                return self._api_day(q)
            if path == "/api/race":
                return self._api_race(q)
            if path == "/api/signs":
                return self._api_signs(q)
            if path == "/api/integrity":
                return self._api_integrity()
            if path.startswith("/api/job/"):
                jid = path[len("/api/job/"):]
                j = _job_get(jid)
                if not j:
                    return _json_response(self, 404, {"error": "not found"})
                return _json_response(self, 200, j)
            return _json_response(self, 404, {"error": "not found", "path": path})
        except Exception as e:
            import traceback; traceback.print_exc()
            return _json_response(self, 500, {"error": str(e)})

    # ── POST ───────────────────────────────────────────────────
    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = _read_json_body(self)
        try:
            if path == "/api/collect":
                return self._api_collect(body)
            if path == "/api/mine":
                return self._api_mine(body)
            if path == "/api/backtest":
                return self._api_backtest(body)
            return _json_response(self, 404, {"error": "not found", "path": path})
        except Exception as e:
            import traceback; traceback.print_exc()
            return _json_response(self, 500, {"error": str(e)})

    # ── 個別実装 ───────────────────────────────────────────────
    def _serve_index(self):
        if not os.path.exists(INDEX_PATH):
            return _json_response(self, 404, {"error": "index.html not found"})
        with open(INDEX_PATH, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _api_stats(self):
        db.init_db()
        with db.get_conn() as conn:
            stats = db.db_stats(conn)
        return _json_response(self, 200, stats)

    def _api_today(self):
        date = datetime.date.today().strftime("%Y%m%d")
        return self._api_day({"date": [date]})

    def _api_day(self, q):
        date = (q.get("date") or [""])[0]
        if not date:
            return _json_response(self, 400, {"error": "date required"})
        venues = scraper.fetch_daily_venues(date)
        ladies = [v for v in venues if v["is_ladies"]]
        # DB に保存済みなら使う
        db.init_db()
        with db.get_conn() as conn:
            races_in_db = list(conn.execute(
                "SELECT date, jcd, rno, venue, title, is_ladies, has_card, has_result FROM races WHERE date=? AND is_ladies=1 ORDER BY jcd, rno",
                (date,),
            ))
        return _json_response(self, 200, {
            "date": date,
            "venues": venues,
            "ladies_venues": ladies,
            "races_in_db": [dict(r) for r in races_in_db],
        })

    def _api_race(self, q):
        date = (q.get("date") or [""])[0]
        jcd = (q.get("jcd") or [""])[0]
        rno_str = (q.get("rno") or [""])[0]
        if not (date and jcd and rno_str):
            return _json_response(self, 400, {"error": "date, jcd, rno required"})
        rno = int(rno_str)
        db.init_db()
        with db.get_conn() as conn:
            # 出走表（DBに無ければスクレイプ）
            row = conn.execute(
                "SELECT has_card, has_result, is_ladies, title, venue FROM races WHERE date=? AND jcd=? AND rno=?",
                (date, jcd, rno),
            ).fetchone()
            if not row or not row["has_card"]:
                card = scraper.fetch_racecard(date, jcd, rno)
                if card.entries:
                    db.save_racecard(conn, card)
                    conn.commit()
            entries = list(conn.execute(
                "SELECT * FROM race_entries WHERE date=? AND jcd=? AND rno=? ORDER BY lane",
                (date, jcd, rno),
            ))
            # 発火サイン
            card_entries = [(e["lane"], e["toban"]) for e in entries]
            fires = miner.find_fires_for_card(conn, card_entries)
            # 結果（あれば）
            result = conn.execute(
                "SELECT * FROM race_results WHERE date=? AND jcd=? AND rno=?",
                (date, jcd, rno),
            ).fetchone()
            race_row = conn.execute(
                "SELECT date, jcd, rno, venue, title, is_ladies, has_card, has_result FROM races WHERE date=? AND jcd=? AND rno=?",
                (date, jcd, rno),
            ).fetchone()

        return _json_response(self, 200, {
            "race": dict(race_row) if race_row else None,
            "entries": [dict(e) for e in entries],
            "fires": fires,
            "result": dict(result) if result else None,
        })

    def _api_signs(self, q):
        limit = int((q.get("limit") or ["200"])[0])
        kind = (q.get("kind") or [""])[0]
        db.init_db()
        with db.get_conn() as conn:
            sql = "SELECT * FROM signs"
            params = []
            if kind:
                sql += " WHERE target_kind=?"
                params.append(kind)
            sql += " ORDER BY confidence DESC, support DESC LIMIT ?"
            params.append(limit)
            rows = list(conn.execute(sql, params))
        return _json_response(self, 200, {
            "count": len(rows),
            "signs": [dict(r) for r in rows],
        })

    def _api_integrity(self):
        db.init_db()
        with db.get_conn() as conn:
            issues = backtest.check_integrity(conn)
        return _json_response(self, 200, issues)

    def _api_collect(self, body):
        start = body.get("start")
        end = body.get("end")
        force = bool(body.get("force"))
        if not (start and end):
            return _json_response(self, 400, {"error": "start, end required"})
        jid = uuid.uuid4().hex[:12]
        _job_set(jid, status="running", started_at=datetime.datetime.now().isoformat(),
                 kind="collect", start=start, end=end)

        def _run():
            try:
                import collect as collector
                def prog(line):
                    _job_log(jid, line)
                r = collector.collect_range(start, end, force=force, progress=prog)
                _job_set(jid, status="done", result=r,
                         finished_at=datetime.datetime.now().isoformat())
            except Exception as e:
                import traceback; traceback.print_exc()
                _job_set(jid, status="error", error=str(e),
                         finished_at=datetime.datetime.now().isoformat())

        threading.Thread(target=_run, daemon=True).start()
        return _json_response(self, 200, {"job_id": jid})

    def _api_mine(self, body):
        params = {
            "min_support": int(body.get("min_support", 10)),
            "min_confidence": float(body.get("min_confidence", 0.80)),
            "min_lift": float(body.get("min_lift", 1.5)),
            "max_p_value": float(body.get("max_p_value", 0.05)),
        }
        db.init_db()
        with db.get_conn() as conn:
            races = miner.load_races(conn, ladies_only=True)
            signs = miner.mine_signs(races, **params)
            n = miner.save_signs(conn, signs)
        return _json_response(self, 200, {
            "races": len(races),
            "signs_extracted": n,
            "params": params,
            "top10": signs[:10],
        })

    def _api_backtest(self, body):
        today = datetime.date.today()
        test_to = body.get("test_to") or today.strftime("%Y%m%d")
        test_from = body.get("test_from") or (today - datetime.timedelta(days=90)).strftime("%Y%m%d")
        train_to_default = (datetime.datetime.strptime(test_from, "%Y%m%d").date()
                            - datetime.timedelta(days=1)).strftime("%Y%m%d")
        train_to = body.get("train_to") or train_to_default
        train_from = body.get("train_from") or "20000101"
        params = {
            "min_support": int(body.get("min_support", 10)),
            "min_confidence": float(body.get("min_confidence", 0.80)),
            "min_lift": float(body.get("min_lift", 1.5)),
            "max_p_value": float(body.get("max_p_value", 0.05)),
        }
        db.init_db()
        with db.get_conn() as conn:
            r = backtest.run_backtest(conn,
                train_from=train_from, train_to=train_to,
                test_from=test_from, test_to=test_to, **params)
        return _json_response(self, 200, r)


# ── 起動時オートブートストラップ ─────────────────────────────────
def _auto_bootstrap():
    """
    起動時、DBが空ならバックグラウンドで直近 AUTO_INIT_DAYS 日分を自動収集する。
    Render 等のエフェメラルディスク環境向け。
    """
    days = int(os.environ.get("AUTO_INIT_DAYS", "0"))
    if days <= 0:
        return
    try:
        with db.get_conn() as conn:
            row = conn.execute("SELECT COUNT(*) FROM races").fetchone()
        if row[0] > 0:
            print(f"[bootstrap] DBに既存データあり (races={row[0]})。スキップ")
            return
    except Exception as e:
        print(f"[bootstrap] DB確認失敗: {e}")
        return

    def _run():
        try:
            import collect as collector
            today = datetime.date.today()
            start = (today - datetime.timedelta(days=days - 1)).strftime("%Y%m%d")
            end = today.strftime("%Y%m%d")
            print(f"[bootstrap] DB空 → 自動収集開始 ({start}〜{end})")
            r = collector.collect_range(start, end, force=False,
                                        progress=lambda s: print(f"[bootstrap] {s}"))
            print(f"[bootstrap] 完了: {r}")
        except Exception as e:
            import traceback; traceback.print_exc()
            print(f"[bootstrap] エラー: {e}")

    threading.Thread(target=_run, daemon=True).start()


# ── 起動 ─────────────────────────────────────────────────────────
def main():
    db.init_db()
    print(f"╭─────────────────────────────────────╮")
    print(f"│  競艇サインマイナー サーバー         │")
    print(f"│  http://0.0.0.0:{PORT}/               │")
    print(f"╰─────────────────────────────────────╯")
    _auto_bootstrap()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n停止しました")


if __name__ == "__main__":
    main()
