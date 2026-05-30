#!/usr/bin/env python3
"""
オカルト/異常値 複合サイン候補の生存追跡（READ-ONLY・CLI）。

評価ロジックは watchlist_eval.evaluate() に集約（サーバーの /api/watchlist と同一）。
occult_watchlist.json の各候補について現在の DB 全体で support/hits/的中率/lift を
再計算し、発見時 baseline と比較して 生存/黄信号/淘汰 を判定する。

  dim=異常lift … lift基準(生存>=2.0倍)   それ以外 … 的中率基準(生存>=80%)

使い方:  python3 occult_watchlist.py
"""
import sqlite3
import os
import watchlist_eval

BASE = os.path.dirname(os.path.abspath(__file__))
con = sqlite3.connect(f"file:{os.path.join(BASE, 'kyotei_sign.db')}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
ev = watchlist_eval.evaluate(con)
con.close()

print(f"=== オカルト/異常値サイン 生存追跡 ===  対象レース(女子戦・結果あり)={ev['total_races']}")
print(f"記録日={ev['recorded_at']}  基準: 的中率系 生存>={ev['survive_rate']*100:.0f}%/黄>={ev['watch_rate']*100:.0f}% ・ lift系 生存>=2.0倍")
print(f"{'id':>2} {'dim':<7} {'条件(AND)':<36} {'2艇':>4} {'基準':>7} {'現在':>9} {'伸':>4} {'率':>5} {'lift':>5} 判定")
for r in ev["rows"]:
    a, b = r["pair"]
    grow = r["growth"]
    grows = f"+{grow}" if grow > 0 else ("0" if grow == 0 else str(grow))
    print(f"{r['id']:>2} {r['dim']:<7} {r['desc']:<36} {a}-{b} "
          f"{r['base_hits']:>2}/{r['base_support']:<3} {r['hits']:>2}/{r['support']:<5} "
          f"{grows:>4} {r['rate']*100:4.0f}% {r['lift']:4.1f}倍 {r['verdict']}")
