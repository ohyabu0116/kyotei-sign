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
  GET  /api/watchlist         オカルト/異常値ウォッチリストの生存追跡
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
import saver
import watchlist_eval
import composite

APP_VERSION = "2.8"
PORT = int(os.environ.get("PORT", 8772))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(BASE_DIR, "index.html")

# ── ジョブ管理 ───────────────────────────────────────────────────
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_collect_lock = threading.Lock()    # 同時に1つのデータ収集ジョブだけ走らせる
_active_collect_job: dict = {"id": None}


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

    # ── OPTIONS (CORSプリフライト) ─────────────────────────────
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    # ── GET ────────────────────────────────────────────────────
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        q = parse_qs(parsed.query)

        try:
            if path == "/" or path == "/index.html":
                return self._serve_index()
            if path == "/api/ping":
                return _json_response(self, 200, {"ok": True, "ts": time.time(), "version": APP_VERSION})
            if path == "/api/version":
                return _json_response(self, 200, {"version": APP_VERSION})
            if path == "/api/stats":
                return self._api_stats()
            if path == "/api/today":
                return self._api_today()
            if path == "/api/day":
                return self._api_day(q)
            if path == "/api/race":
                return self._api_race(q)
            if path == "/api/results":
                return self._api_results(q)
            if path == "/api/watchlist":
                return self._api_watchlist()
            if path == "/api/signs":
                return self._api_signs(q)
            if path == "/api/sign_detail":
                return self._api_sign_detail(q)
            if path == "/api/racer_search":
                return self._api_racer_search(q)
            if path == "/api/integrity":
                return self._api_integrity()
            if path == "/api/export":
                return self._api_export()
            if path == "/api/save":
                r = saver.save_now(reason="api")
                return _json_response(self, 200 if r.get("ok") else 500, r)
            if path == "/api/reload":
                r = saver.sync_from_remote()
                return _json_response(self, 200 if r.get("ok") else 500, r)
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

        # 複合予想エンジン（多角的な異常値を合成した予想の軸）
        predict = None
        if entries:
            try:
                meta = {"date": date, "rno": rno,
                        "venue": race_row["venue"] if race_row else ""}
                predict = composite.predict_race(entries, meta)
            except Exception as e:
                import traceback; traceback.print_exc()
                predict = {"ok": False, "error": str(e)}

        return _json_response(self, 200, {
            "race": dict(race_row) if race_row else None,
            "entries": [dict(e) for e in entries],
            "fires": fires,
            "predict": predict,
            "result": dict(result) if result else None,
        })

    def _api_results(self, q):
        """結果一覧: 指定日の女子戦の結果（着順・決まり手・配当）。日付なしなら直近 limit 件。"""
        date = (q.get("date") or [""])[0]
        limit = int((q.get("limit") or ["60"])[0])
        db.init_db()
        with db.get_conn() as conn:
            sql = """SELECT r.date, r.jcd, r.venue, r.rno, rr.finish_json, rr.determinant,
                            rr.payout_3t, rr.payout_3t_yen, rr.payout_3f, rr.payout_3f_yen
                     FROM races r JOIN race_results rr
                       ON r.date=rr.date AND r.jcd=rr.jcd AND r.rno=rr.rno
                     WHERE r.is_ladies=1"""
            params: list = []
            if date:
                sql += " AND r.date=? ORDER BY r.jcd, r.rno"
                params.append(date)
            else:
                sql += " ORDER BY r.date DESC, r.jcd, r.rno LIMIT ?"
                params.append(limit)
            rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            try:
                fin = json.loads(r["finish_json"])
            except Exception:
                fin = []
            out.append({
                "date": r["date"], "jcd": r["jcd"], "venue": r["venue"], "rno": r["rno"],
                "finish": [[f[0], f[1]] for f in fin[:3]],  # [[着, 艇番], ...]
                "determinant": r["determinant"],
                "payout_3t": r["payout_3t"], "payout_3t_yen": r["payout_3t_yen"],
                "payout_3f": r["payout_3f"], "payout_3f_yen": r["payout_3f_yen"],
            })
        return _json_response(self, 200, {"count": len(out), "date": date, "results": out})

    def _api_watchlist(self):
        """オカルト/異常値ウォッチリスト: 各候補の現在 support/的中率/lift/判定を返す。"""
        db.init_db()
        with db.get_conn() as conn:
            data = watchlist_eval.evaluate(conn)
        return _json_response(self, 200, data)

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

    def _api_sign_detail(self, q):
        toban = (q.get("toban") or [""])[0]
        lane = int((q.get("lane") or ["0"])[0])
        target = (q.get("target") or [""])[0]
        kind = (q.get("kind") or [""])[0]
        if not (toban and lane and target and kind):
            return _json_response(self, 400, {"error": "toban, lane, target, kind required"})
        db.init_db()
        with db.get_conn() as conn:
            instances = miner.sign_instances(conn, toban, lane, target, kind)
        return _json_response(self, 200, {
            "toban": toban, "cond_lane": lane, "target": target, "kind": kind,
            "count": len(instances),
            "hits": sum(1 for i in instances if i["hit"]),
            "instances": instances,
        })

    def _api_racer_search(self, q):
        """注目選手の大捜索: 候補サイン一覧 + 各候補の事例(3連単配当付き)を返す。"""
        toban = (q.get("toban") or [""])[0]
        if not toban:
            return _json_response(self, 400, {"error": "toban required"})
        min_support = int((q.get("min_support") or ["3"])[0])
        min_confidence = float((q.get("min_confidence") or ["0.6"])[0])
        db.init_db()
        with db.get_conn() as conn:
            res = miner.racer_search(conn, toban,
                                     min_support=min_support,
                                     min_confidence=min_confidence)
            # 各候補に事例を付与（既存 sign_instances を再利用 → 3連単配当を含む）
            for c in res["candidates"]:
                instances = miner.sign_instances(
                    conn, c["toban"], c["cond_lane"],
                    c["target_pair"], c["target_kind"])
                c["instances"] = instances
        return _json_response(self, 200, res)

    def _api_integrity(self):
        db.init_db()
        with db.get_conn() as conn:
            issues = backtest.check_integrity(conn)
        return _json_response(self, 200, issues)

    def _api_export(self):
        """全DB内容をJSONで返す。GitHub Actions が定期的に叩いてバックアップする。"""
        db.init_db()
        with db.get_conn() as conn:
            data = {
                "exported_at": datetime.datetime.now().isoformat(),
                "schema_version": 1,
                "races": [dict(r) for r in conn.execute("SELECT * FROM races")],
                "race_entries": [dict(r) for r in conn.execute("SELECT * FROM race_entries")],
                "race_results": [dict(r) for r in conn.execute("SELECT * FROM race_results")],
                "signs": [dict(r) for r in conn.execute("SELECT * FROM signs")],
            }
        return _json_response(self, 200, data)

    def _api_collect(self, body):
        start = body.get("start")
        end = body.get("end")
        force = bool(body.get("force"))
        if not (start and end):
            return _json_response(self, 400, {"error": "start, end required"})

        # 既に他のジョブが走っているなら拒否
        if not _collect_lock.acquire(blocking=False):
            existing = _active_collect_job.get("id")
            return _json_response(self, 409, {
                "error": "another collect job is running",
                "existing_job_id": existing,
            })

        jid = uuid.uuid4().hex[:12]
        _active_collect_job["id"] = jid
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
            finally:
                _active_collect_job["id"] = None
                _collect_lock.release()

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


# ── 起動時の data ブランチ data.json 復元 ────────────────────────
# main ブランチ更新だと Render が自動再デプロイされてしまうので
# スナップショットは data ブランチに分離している
DATA_URL = "https://raw.githubusercontent.com/ohyabu0116/kyotei-sign/data/data.json"
DATA_URL_FALLBACK = "https://raw.githubusercontent.com/ohyabu0116/kyotei-sign/main/data.json"


def _restore_from_repo():
    """
    起動時、DBが空なら data ブランチの data.json から復元する。
    main にもあるなら fallback として使う。
    """
    try:
        with db.get_conn() as conn:
            row = conn.execute("SELECT COUNT(*) FROM races").fetchone()
        if row[0] > 0:
            print(f"[restore] DBに既存データあり (races={row[0]})。復元スキップ")
            return False
    except Exception as e:
        print(f"[restore] DB確認失敗: {e}")

    import urllib.request
    payload = None
    for url in (DATA_URL, DATA_URL_FALLBACK):
        try:
            print(f"[restore] data.json fetch: {url}")
            req = urllib.request.Request(url, headers={"User-Agent": "kyotei-sign"})
            with urllib.request.urlopen(req, timeout=60) as r:
                payload = json.loads(r.read())
            print(f"[restore] OK: {url}")
            break
        except Exception as e:
            print(f"[restore] {url} 失敗 ({e})")
    if payload is None:
        print(f"[restore] 全URL失敗 → bootstrap にフォールバック")
        return False

    counts = {}
    try:
        with db.get_conn() as conn:
            for tbl in ("races", "race_entries", "race_results", "signs"):
                rows = payload.get(tbl, [])
                counts[tbl] = len(rows)
                if not rows:
                    continue
                cols = list(rows[0].keys())
                col_str = ", ".join(cols)
                ph = ", ".join("?" * len(cols))
                for row in rows:
                    conn.execute(
                        f"INSERT OR REPLACE INTO {tbl} ({col_str}) VALUES ({ph})",
                        tuple(row.get(c) for c in cols),
                    )
            conn.commit()
        print(f"[restore] 復元完了: {counts}")
        return True
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[restore] DB書き込み失敗: {e}")
        return False


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
        # 同時にUI/APIから収集が走らないようロックを取る
        if not _collect_lock.acquire(blocking=False):
            print("[bootstrap] 別の収集ジョブが既に走っている → スキップ")
            return
        try:
            jid = "bootstrap"
            _active_collect_job["id"] = jid
            _job_set(jid, status="running", kind="bootstrap")
            import collect as collector
            today = datetime.date.today()
            start = (today - datetime.timedelta(days=days - 1)).strftime("%Y%m%d")
            end = today.strftime("%Y%m%d")
            print(f"[bootstrap] DB空 → 自動収集開始 ({start}〜{end})")
            r = collector.collect_range(start, end, force=False,
                                        progress=lambda s: print(f"[bootstrap] {s}"))
            print(f"[bootstrap] 完了: {r}")
            _job_set(jid, status="done", result=r)
        except Exception as e:
            import traceback; traceback.print_exc()
            print(f"[bootstrap] エラー: {e}")
            _job_set("bootstrap", status="error", error=str(e))
        finally:
            _active_collect_job["id"] = None
            _collect_lock.release()

    threading.Thread(target=_run, daemon=True).start()


# ── 定期リフレッシュ（新しい日のレースを自動取得）───────────────
def _daily_refresh_loop():
    """
    一定間隔で直近 REFRESH_DAYS 日分を再収集する。
    - 新しい開催日の出走表を取得
    - レース確定後の結果を取得（force=Trueで結果を更新）
    これで「今日以降のレース」も自動で増えていく。
    """
    interval_h = float(os.environ.get("REFRESH_INTERVAL_HOURS", "3"))
    days = int(os.environ.get("REFRESH_DAYS", "3"))
    if interval_h <= 0:
        print("[refresh] 無効 (REFRESH_INTERVAL_HOURS=0)")
        return

    def _run():
        print(f"[refresh] 開始: {interval_h}時間ごとに直近{days}日を再収集")
        # 起動直後は他の処理と被らないよう少し待つ
        time.sleep(300)
        while True:
            if _collect_lock.acquire(blocking=False):
                jid = "refresh"
                _active_collect_job["id"] = jid
                _job_set(jid, status="running", kind="refresh")
                try:
                    import collect as collector
                    today = datetime.date.today()
                    start = (today - datetime.timedelta(days=days - 1)).strftime("%Y%m%d")
                    end = today.strftime("%Y%m%d")
                    print(f"[refresh] 直近{days}日を再収集 ({start}〜{end})")
                    r = collector.collect_range(start, end, force=True,
                                                progress=lambda s: print(f"[refresh] {s}"))
                    print(f"[refresh] 完了: {r}")
                    _job_set(jid, status="done", result=r)
                except Exception as e:
                    import traceback; traceback.print_exc()
                    _job_set("refresh", status="error", error=str(e))
                finally:
                    _active_collect_job["id"] = None
                    _collect_lock.release()
            else:
                print("[refresh] 別ジョブ走行中 → 今回はスキップ")
            time.sleep(interval_h * 3600)

    threading.Thread(target=_run, daemon=True).start()


# ── 起動 ─────────────────────────────────────────────────────────
def main():
    db.init_db()
    print(f"╭─────────────────────────────────────╮")
    print(f"│  競艇サインマイナー サーバー         │")
    print(f"│  http://0.0.0.0:{PORT}/               │")
    print(f"╰─────────────────────────────────────╯")
    # まず data.json から復元を試みる。空ならbootstrap
    restored = _restore_from_repo()
    if not restored:
        _auto_bootstrap()
    # サーバー自身による定期GitHub保存 + dataブランチ同期
    saver.start_autosave_loop()
    # 新しい日のレースを定期的に自動取得
    _daily_refresh_loop()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n停止しました")


if __name__ == "__main__":
    main()
