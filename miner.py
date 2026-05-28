"""
サインマイナー

オカルト型サインを統計的に検出する。

【サインの形】
  条件部 (LHS):  選手X が N号艇に乗る
  結果部 (RHS):  M号艇 と P号艇 が両方3着以内    （target_kind='top3_pair'）
                 または M号艇 が1着             （target_kind='win'）

【採用基準（厳しめ）】
  - support     >= 10
  - confidence  >= 0.80
  - lift        >= 1.5
  - p-value     <  0.05  （二項検定: 観測した的中率 > 母比率 を片側検定）

実装は1パススキャン。約2年分の女子戦データなら数秒で終わる。
"""
from __future__ import annotations

import json
import math
import datetime
from collections import defaultdict
from typing import Iterable

import db


# ── 統計関数（標準ライブラリのみで実装） ─────────────────────────
def _log_binomial_coef(n: int, k: int) -> float:
    """log(nCk)"""
    if k < 0 or k > n:
        return float("-inf")
    if k == 0 or k == n:
        return 0.0
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def binom_sf(k: int, n: int, p: float) -> float:
    """P(X >= k) when X ~ Binomial(n, p). 片側生存関数。"""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    if p <= 0:
        return 0.0 if k > 0 else 1.0
    if p >= 1:
        return 1.0
    # 直接和。n は最大 ~数千なので問題なし
    log_p = math.log(p)
    log_1p = math.log1p(-p)
    total = 0.0
    for i in range(k, n + 1):
        log_term = _log_binomial_coef(n, i) + i * log_p + (n - i) * log_1p
        total += math.exp(log_term)
        if math.exp(log_term) < 1e-15 and i > k + 50:
            break
    return min(max(total, 0.0), 1.0)


# ── ロード ───────────────────────────────────────────────────────
def load_races(conn, *, ladies_only: bool = True,
               date_from: str | None = None,
               date_to: str | None = None) -> list[dict]:
    """
    結果まで揃っているレースを取得。各レコードは
      {date, jcd, rno, entries: [(lane, toban, name), ...], finish: [(rank, lane, toban, name), ...]}
    """
    sql = """
        SELECT r.date, r.jcd, r.rno, r.title, rr.finish_json
        FROM races r
        JOIN race_results rr ON r.date=rr.date AND r.jcd=rr.jcd AND r.rno=rr.rno
        WHERE r.has_card=1 AND r.has_result=1
    """
    params: list = []
    if ladies_only:
        sql += " AND r.is_ladies=1"
    if date_from:
        sql += " AND r.date >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND r.date <= ?"
        params.append(date_to)
    sql += " ORDER BY r.date, r.jcd, r.rno"

    races = []
    for row in conn.execute(sql, params):
        date, jcd, rno = row["date"], row["jcd"], row["rno"]
        entries = list(conn.execute(
            "SELECT lane, toban, name FROM race_entries WHERE date=? AND jcd=? AND rno=? ORDER BY lane",
            (date, jcd, rno),
        ))
        finish = json.loads(row["finish_json"])
        races.append({
            "date": date, "jcd": jcd, "rno": rno,
            "title": row["title"],
            "entries": [(e["lane"], e["toban"], e["name"]) for e in entries],
            "finish": finish,
        })
    return races


# ── マイニング ───────────────────────────────────────────────────
def mine_signs(races: list[dict], *,
               min_support: int = 10,
               min_confidence: float = 0.80,
               min_lift: float = 1.5,
               max_p_value: float = 0.05) -> list[dict]:
    """
    全レースを1パスで走査し、サインを抽出して返す。
    """
    if not races:
        return []

    # 集計用
    support: dict[tuple, int] = defaultdict(int)   # (toban, lane) -> 出走回数
    hits_pair: dict[tuple, int] = defaultdict(int) # (toban, lane, (a,b)) -> ヒット数
    hits_win: dict[tuple, int] = defaultdict(int)  # (toban, lane, m)    -> 勝った数

    # 母比率算出のための全体集計
    global_pair: dict[tuple[int, int], int] = defaultdict(int)
    global_win: dict[int, int] = defaultdict(int)
    total_races = 0
    toban_name: dict[str, str] = {}

    for race in races:
        top3 = {f[1] for f in race["finish"][:3]}
        top1 = race["finish"][0][1] if race["finish"] else None
        if len(top3) < 3 or top1 is None:
            continue
        total_races += 1

        # グローバル集計
        for a in range(1, 7):
            for b in range(a + 1, 7):
                if a in top3 and b in top3:
                    global_pair[(a, b)] += 1
        global_win[top1] += 1

        # 各艇ごと
        for lane, toban, name in race["entries"]:
            if not toban:
                continue
            toban_name[toban] = name
            support[(toban, lane)] += 1
            for a in range(1, 7):
                for b in range(a + 1, 7):
                    if a in top3 and b in top3:
                        hits_pair[(toban, lane, (a, b))] += 1
            hits_win[(toban, lane, top1)] += 1

    # 母比率
    base_pair = {ab: cnt / total_races for ab, cnt in global_pair.items()}
    base_win = {m: cnt / total_races for m, cnt in global_win.items()}

    signs: list[dict] = []

    # top3_pair
    for (toban, lane, ab), hits in hits_pair.items():
        sup = support[(toban, lane)]
        if sup < min_support:
            continue
        conf = hits / sup
        if conf < min_confidence:
            continue
        base = base_pair.get(ab, 1e-9)
        lift = conf / base if base > 0 else float("inf")
        if lift < min_lift:
            continue
        p_val = binom_sf(hits, sup, base)
        if p_val >= max_p_value:
            continue
        signs.append({
            "toban": toban,
            "racer_name": toban_name.get(toban, ""),
            "cond_lane": lane,
            "target_pair": f"{ab[0]}-{ab[1]}",
            "target_kind": "top3_pair",
            "support": sup,
            "hits": hits,
            "confidence": round(conf, 4),
            "lift": round(lift, 3),
            "p_value": round(p_val, 5),
        })

    # win
    for (toban, lane, m), hits in hits_win.items():
        sup = support[(toban, lane)]
        if sup < min_support:
            continue
        conf = hits / sup
        if conf < min_confidence:
            continue
        base = base_win.get(m, 1e-9)
        lift = conf / base if base > 0 else float("inf")
        if lift < min_lift:
            continue
        p_val = binom_sf(hits, sup, base)
        if p_val >= max_p_value:
            continue
        signs.append({
            "toban": toban,
            "racer_name": toban_name.get(toban, ""),
            "cond_lane": lane,
            "target_pair": str(m),
            "target_kind": "win",
            "support": sup,
            "hits": hits,
            "confidence": round(conf, 4),
            "lift": round(lift, 3),
            "p_value": round(p_val, 5),
        })

    # ソート: confidence DESC, support DESC
    signs.sort(key=lambda s: (-s["confidence"], -s["support"]))
    return signs


def save_signs(conn, signs: list[dict]) -> int:
    """signs テーブルを全置換"""
    now = datetime.datetime.now().isoformat()
    conn.execute("DELETE FROM signs")
    for s in signs:
        conn.execute("""
            INSERT INTO signs(toban, racer_name, cond_lane, target_pair, target_kind,
                support, hits, confidence, lift, p_value, discovered_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (s["toban"], s["racer_name"], s["cond_lane"],
              s["target_pair"], s["target_kind"],
              s["support"], s["hits"], s["confidence"],
              s["lift"], s["p_value"], now))
    conn.commit()
    return len(signs)


# ── 予想 ─────────────────────────────────────────────────────────
def sign_instances(conn, toban: str, cond_lane: int,
                   target_pair: str, target_kind: str) -> list[dict]:
    """
    あるサインが「いつ・どこの競艇場・何レースで」観測されたかの事例一覧を返す。
    各事例: { date, venue, jcd, rno, finish(着順上位3艇), hit(的中したか) }
    """
    rows = conn.execute("""
        SELECT e.date, e.jcd, e.rno, r.venue, r.title, rr.finish_json,
               rr.payout_3t, rr.payout_3t_yen
        FROM race_entries e
        JOIN races r ON e.date=r.date AND e.jcd=r.jcd AND e.rno=r.rno
        JOIN race_results rr ON e.date=rr.date AND e.jcd=rr.jcd AND e.rno=rr.rno
        WHERE e.toban=? AND e.lane=? AND r.is_ladies=1
        ORDER BY e.date, e.jcd, e.rno
    """, (toban, cond_lane)).fetchall()

    out = []
    for row in rows:
        fin = json.loads(row["finish_json"])
        top3 = {f[1] for f in fin[:3]}
        top1 = fin[0][1] if fin else None
        if target_kind == "win":
            hit = (top1 == int(target_pair))
        else:
            a, b = (int(x) for x in target_pair.split("-"))
            hit = (a in top3 and b in top3)
        out.append({
            "date": row["date"],
            "venue": row["venue"],
            "jcd": row["jcd"],
            "rno": row["rno"],
            "title": row["title"],
            "finish": [list(f) for f in fin[:3]],   # [[着,艇,登番,名前],...]
            "hit": hit,
            "payout_3t": row["payout_3t"],           # 3連単の組（例 "1-2-3"）
            "payout_3t_yen": row["payout_3t_yen"],   # 3連単配当（円）
        })
    return out


def find_fires_for_card(conn, card_entries: list[tuple[int, str]]) -> list[dict]:
    """
    出走表 entries=[(lane, toban), ...] に対して発火するサインを抽出。
    """
    if not card_entries:
        return []
    fires = []
    for lane, toban in card_entries:
        rows = conn.execute("""
            SELECT * FROM signs
            WHERE toban=? AND cond_lane=?
            ORDER BY confidence DESC, support DESC
        """, (toban, lane)).fetchall()
        for r in rows:
            fires.append(dict(r))
    return fires


if __name__ == "__main__":
    import sys
    db.init_db()
    with db.get_conn() as conn:
        races = load_races(conn, ladies_only=True)
        print(f"対象レース: {len(races)}")
        signs = mine_signs(races)
        print(f"抽出サイン: {len(signs)}")
        for s in signs[:20]:
            print(f"  {s['racer_name']}({s['toban']})@{s['cond_lane']}号艇 → "
                  f"{s['target_kind']}={s['target_pair']}  "
                  f"{s['hits']}/{s['support']}={s['confidence']:.0%}  "
                  f"lift={s['lift']}  p={s['p_value']}")
        if "--save" in sys.argv:
            n = save_signs(conn, signs)
            print(f"DBに{n}件保存しました")
