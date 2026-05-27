#!/usr/bin/env python3
"""
SQLite DB → data.json を生成。

使い方:
  python3 export_data.py                # ローカルDBから（kyotei_sign.db）
  python3 export_data.py --from-render  # Render の /api/export から取得

このスクリプトの出力 data.json を git commit + push すると、
Render が再デプロイされたときに自動で復元される。
（server.py の _restore_from_repo() が data.json を読みに行く）

keirin-sign の export_data.py と同じパターン。
"""
import json
import os
import sqlite3
import sys
import urllib.request
import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "kyotei_sign.db")
OUT = os.path.join(BASE, "data.json")
RENDER_URL = "https://kyotei-sign.onrender.com/api/export"


def export_from_local() -> dict | None:
    if not os.path.exists(DB):
        print(f"[local] DB not found: {DB}")
        return None
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    data = {
        "exported_at": datetime.datetime.now().isoformat(),
        "schema_version": 1,
        "source": "local",
    }
    for tbl in ("races", "race_entries", "race_results", "signs"):
        rows = conn.execute(f"SELECT * FROM {tbl}").fetchall()
        data[tbl] = [dict(r) for r in rows]
        print(f"  {tbl}: {len(rows)}件")
    conn.close()
    return data


def export_from_render() -> dict:
    print(f"[render] Fetching {RENDER_URL}...")
    req = urllib.request.Request(RENDER_URL, headers={"User-Agent": "kyotei-sign-export"})
    with urllib.request.urlopen(req, timeout=180) as r:
        data = json.loads(r.read())
    data["source"] = "render"
    for tbl in ("races", "race_entries", "race_results", "signs"):
        print(f"  {tbl}: {len(data.get(tbl, []))}件")
    return data


def main():
    args = sys.argv[1:]
    use_render = "--from-render" in args

    if use_render:
        data = export_from_render()
    else:
        data = export_from_local()
        if data is None or not data.get("races"):
            print("Local DB empty/missing → fallback to Render")
            data = export_from_render()

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

    size_mb = os.path.getsize(OUT) / 1024 / 1024
    print(f"\n✅ Exported → {OUT} ({size_mb:.1f}MB)")
    print(f"\n次のコマンドで Render に永続化:")
    print(f"  git add data.json && git commit -m 'Update data snapshot' && git push")


if __name__ == "__main__":
    main()
