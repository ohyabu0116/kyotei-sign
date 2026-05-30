"""
サーバー自身による GitHub 自動保存。

- 環境変数 GH_TOKEN（repo書き込み権のPAT）が設定されているときだけ有効
- DB全体を JSON 化し、GitHub Contents API で data ブランチの data.json を更新
- 内容が変わっていなければ push しない（ハッシュ比較）
- watchdog や Mac に依存せず、Render 上のプロセスが生きている限り動く

これにより「途中で止まっても直前の保存状態は GitHub に残る」を実現する。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
import urllib.request
import urllib.error

import db

GH_REPO = os.environ.get("GH_REPO", "ohyabu0116/kyotei-sign")
GH_BRANCH = os.environ.get("GH_BACKUP_BRANCH", "data")
GH_PATH = "data.json"
AUTOSAVE_INTERVAL = int(os.environ.get("AUTOSAVE_INTERVAL", "120"))  # 秒

_last_hash: str | None = None
_last_saved_at = 0.0
_save_lock = threading.Lock()


def export_db_dict() -> dict:
    import datetime
    with db.get_conn() as conn:
        return {
            "exported_at": datetime.datetime.now().isoformat(),
            "schema_version": 1,
            "source": "render-autosave",
            "races": [dict(r) for r in conn.execute("SELECT * FROM races")],
            "race_entries": [dict(r) for r in conn.execute("SELECT * FROM race_entries")],
            "race_results": [dict(r) for r in conn.execute("SELECT * FROM race_results")],
            "signs": [dict(r) for r in conn.execute("SELECT * FROM signs")],
        }


def _gh_get_sha(token: str) -> str | None:
    """data ブランチの data.json の現在の SHA を取得（更新に必要）"""
    url = f"https://api.github.com/repos/{GH_REPO}/contents/{GH_PATH}?ref={GH_BRANCH}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "kyotei-sign-saver",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read()).get("sha")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _fetch_remote_payload() -> dict | None:
    """data ブランチの data.json を取得。
    CDNラグ(raw.githubusercontentは最大5分キャッシュ)でアンチクロバー判定が
    狂うのを避けるため、即時反映される GitHub API(生メディア型)を優先し、
    失敗時のみ raw CDN にフォールバックする。"""
    token = os.environ.get("GH_TOKEN")
    api = f"https://api.github.com/repos/{GH_REPO}/contents/{GH_PATH}?ref={GH_BRANCH}"
    headers = {"Accept": "application/vnd.github.raw", "User-Agent": "kyotei-sign-saver"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(api, headers=headers)
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read())
    except Exception:
        pass
    raw = f"https://raw.githubusercontent.com/{GH_REPO}/{GH_BRANCH}/{GH_PATH}"
    try:
        req = urllib.request.Request(raw, headers={"User-Agent": "kyotei-sign-saver"})
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _remote_race_count() -> int:
    """data ブランチの data.json に入っているレース数を取得（無ければ0）"""
    p = _fetch_remote_payload()
    return len(p.get("races", [])) if p else 0


def commit_to_github(token: str, content: str, message: str) -> bool:
    """GitHub Contents API で data ブランチの data.json を更新/作成"""
    sha = _gh_get_sha(token)
    url = f"https://api.github.com/repos/{GH_REPO}/contents/{GH_PATH}"
    payload = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": GH_BRANCH,
    }
    if sha:
        payload["sha"] = sha
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="PUT", headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "kyotei-sign-saver",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status in (200, 201)


def save_now(reason: str = "manual") -> dict:
    """即時保存（内容が変わっていればGitHubへ）。結果を返す。"""
    global _last_hash, _last_saved_at
    token = os.environ.get("GH_TOKEN")
    if not token:
        return {"ok": False, "reason": "GH_TOKEN未設定"}

    with _save_lock:
        try:
            data = export_db_dict()
        except Exception as e:
            return {"ok": False, "reason": f"export失敗: {e}"}
        if not data.get("races"):
            return {"ok": False, "reason": "データ空"}

        content = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        h = hashlib.md5(content.encode("utf-8")).hexdigest()
        if h == _last_hash:
            return {"ok": True, "skipped": True, "reason": "変更なし"}

        n_races = len(data["races"])
        # アンチクロバー: リモートの方がレース数が多い場合は上書きしない
        # （Mac側の大規模バックフィルを Render の小さいDBが潰さないように）
        remote = _remote_race_count()
        if n_races < remote:
            return {"ok": True, "skipped": True,
                    "reason": f"リモート({remote})の方が多い→保護", "local": n_races}
        msg = f"Auto-save ({reason}) races={n_races} {time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime())}"
        try:
            commit_to_github(token, content, msg)
            _last_hash = h
            _last_saved_at = time.time()
            return {"ok": True, "races": n_races, "bytes": len(content)}
        except Exception as e:
            return {"ok": False, "reason": f"commit失敗: {e}"}


def _local_race_count() -> int:
    try:
        with db.get_conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM races").fetchone()[0]
    except Exception:
        return 0


def sync_from_remote() -> dict:
    """data ブランチの方がレース数が多ければ取り込む（Macの大量データをミラー）"""
    payload = _fetch_remote_payload()
    if payload is None:
        return {"ok": False, "reason": "fetch失敗"}

    remote = len(payload.get("races", []))
    local = _local_race_count()
    if remote <= local:
        return {"ok": True, "skipped": True, "local": local, "remote": remote}

    try:
        with db.get_conn() as conn:
            for tbl in ("races", "race_entries", "race_results", "signs"):
                rows = payload.get(tbl, [])
                if not rows:
                    continue
                cols = list(rows[0].keys())
                cs = ", ".join(cols)
                ph = ", ".join("?" * len(cols))
                for row in rows:
                    conn.execute(
                        f"INSERT OR REPLACE INTO {tbl} ({cs}) VALUES ({ph})",
                        tuple(row.get(c) for c in cols),
                    )
            conn.commit()
        return {"ok": True, "merged": True, "local_before": local, "remote": remote}
    except Exception as e:
        return {"ok": False, "reason": f"merge失敗: {e}"}


def start_autosave_loop():
    """
    バックグラウンドで定期同期ループを開始。
    - GH_TOKENがあれば: local > remote のとき push（保存）
    - 常時: remote > local のとき pull（ミラー）→ Macの大量データをWebに反映
    """
    token = os.environ.get("GH_TOKEN")

    def _loop():
        print(f"[sync] 開始: {AUTOSAVE_INTERVAL}秒ごとに {GH_BRANCH} と同期 (push={'有' if token else '無'})")
        while True:
            time.sleep(AUTOSAVE_INTERVAL)
            # まず remote の方が多ければ取り込む（Webに最新を反映）
            pull = sync_from_remote()
            if pull.get("merged"):
                print(f"[sync] pull: {pull.get('local_before')}→{pull.get('remote')}")
            # 次に local の方が多ければ push（GH_TOKENがある時のみ）
            if token:
                push = save_now(reason="periodic")
                if push.get("ok") and not push.get("skipped"):
                    print(f"[sync] push: races={push.get('races')}")

    threading.Thread(target=_loop, daemon=True).start()
