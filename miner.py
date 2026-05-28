"""
サインマイナー

オカルト型サインを統計的に検出する。

【サインの形】
  条件部 (LHS):  選手X が N号艇に乗る
  結果部 (RHS):  M号艇 と P号艇 が両方3着以内    （target_kind='top3_pair'）
                 または M号艇 が1着             （target_kind='win'）

【採用基準（厳しめ・多重比較補正あり）】
  - support     >= 10
  - confidence  >= 0.80
  - lift        >= 1.5
  - q-value     <  0.05  （Benjamini-Hochberg FDR 補正後）

  ※ 二項検定 p値は数千の仮説に対して片側検定で算出するため、補正なしでは
    偽陽性が大量に混入する。そこで support>=min_support を満たす全仮説
    （(toban,lane,pair) と (toban,lane,m)）のファミリ全体で BH-FDR を適用し、
    q_value < max_q を採用条件とする。各サインには q_value を付与する。

実装は1パススキャン。約2年分の女子戦データなら数秒で終わる。
"""
from __future__ import annotations

import json
import math
import random
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


def bh_fdr(pvals: list[float]) -> list[float]:
    """
    Benjamini-Hochberg の q値（FDR調整p値）を、入力と同じ順序で返す。

    pvals: list[float]
    returns: list[float]  各仮説の q値（単調化済み・[0,1]にクリップ）
    """
    m = len(pvals)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvals[i])
    q = [0.0] * m
    prev = 1.0
    # 大きいp値→小さいp値の順で単調化（q_(k) = min over j>=k of p_(j)*m/j）
    for rank in range(m - 1, -1, -1):
        i = order[rank]
        val = pvals[i] * m / (rank + 1)
        prev = min(prev, val)
        q[i] = min(prev, 1.0)
    return q


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
def _scan_family(races: list[dict], min_support: int) -> tuple[list[dict], dict[str, str]]:
    """
    全レースを1パス走査し、support>=min_support を満たす検定仮説ファミリ全体を返す。

    返すのは「フィルタ前」の全仮説（(toban,lane,pair) と (toban,lane,m)）。
    各仮説 dict: {toban, cond_lane, target_pair, target_kind, support, hits,
                  confidence, lift, p_value}
    BH-FDR はこのファミリ全体に対して掛けるのが正しい（採用フィルタ後ではない）。

    returns: (candidates, toban_name)
    """
    support: dict[tuple, int] = defaultdict(int)   # (toban, lane) -> 出走回数
    hits_pair: dict[tuple, int] = defaultdict(int)  # (toban, lane, (a,b)) -> ヒット数
    hits_win: dict[tuple, int] = defaultdict(int)   # (toban, lane, m)    -> 勝った数

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

    if total_races == 0:
        return [], toban_name

    # 母比率
    base_pair = {ab: cnt / total_races for ab, cnt in global_pair.items()}
    base_win = {m: cnt / total_races for m, cnt in global_win.items()}

    candidates: list[dict] = []

    # top3_pair（support>=min_support の全仮説）
    for (toban, lane, ab), hits in hits_pair.items():
        sup = support[(toban, lane)]
        if sup < min_support:
            continue
        conf = hits / sup
        base = base_pair.get(ab, 1e-9)
        lift = conf / base if base > 0 else float("inf")
        p_val = binom_sf(hits, sup, base)
        candidates.append({
            "toban": toban,
            "cond_lane": lane,
            "target_pair": f"{ab[0]}-{ab[1]}",
            "target_kind": "top3_pair",
            "support": sup,
            "hits": hits,
            "confidence": conf,
            "lift": lift,
            "p_value": p_val,
        })

    # win（support>=min_support の全仮説）
    for (toban, lane, m), hits in hits_win.items():
        sup = support[(toban, lane)]
        if sup < min_support:
            continue
        conf = hits / sup
        base = base_win.get(m, 1e-9)
        lift = conf / base if base > 0 else float("inf")
        p_val = binom_sf(hits, sup, base)
        candidates.append({
            "toban": toban,
            "cond_lane": lane,
            "target_pair": str(m),
            "target_kind": "win",
            "support": sup,
            "hits": hits,
            "confidence": conf,
            "lift": lift,
            "p_value": p_val,
        })

    return candidates, toban_name


def mine_signs(races: list[dict], *,
               min_support: int = 10,
               min_confidence: float = 0.80,
               min_lift: float = 1.5,
               max_p_value: float = 0.05,
               max_q: float = 0.05) -> list[dict]:
    """
    全レースを1パスで走査し、多重比較補正(BH-FDR)後のサインを抽出して返す。

    検定する仮説のファミリ全体（support>=min_support を満たす全
    (toban,lane,pair) と (toban,lane,m)）について p値を出し、Benjamini-Hochberg
    FDR で q値を計算。採用条件は「conf>=min_confidence かつ lift>=min_lift
    かつ q_value < max_q」。各サイン dict には q_value を付与する。

    後方互換: 既存引数 max_p_value は維持（補正前の参考フィルタとして併用）。
    返り値は従来通りサインの list。
    """
    if not races:
        return []

    candidates, toban_name = _scan_family(races, min_support)
    if not candidates:
        return []

    # ファミリ全体に BH-FDR を適用して q_value を付与
    qvals = bh_fdr([c["p_value"] for c in candidates])
    for c, q in zip(candidates, qvals):
        c["q_value"] = q

    signs: list[dict] = []
    for c in candidates:
        if c["confidence"] < min_confidence:
            continue
        if c["lift"] < min_lift:
            continue
        # 後方互換: 補正前 p値フィルタも併用（既定 0.05）
        if c["p_value"] >= max_p_value:
            continue
        # 多重比較補正後の採用判定
        if c["q_value"] >= max_q:
            continue
        signs.append({
            "toban": c["toban"],
            "racer_name": toban_name.get(c["toban"], ""),
            "cond_lane": c["cond_lane"],
            "target_pair": c["target_pair"],
            "target_kind": c["target_kind"],
            "support": c["support"],
            "hits": c["hits"],
            "confidence": round(c["confidence"], 4),
            "lift": round(c["lift"], 3),
            "p_value": round(c["p_value"], 5),
            "q_value": round(c["q_value"], 5),
        })

    # ソート: confidence DESC, support DESC
    signs.sort(key=lambda s: (-s["confidence"], -s["support"]))
    return signs


def placebo_eval(races: list[dict], *,
                 min_support: int = 10,
                 min_confidence: float = 0.80,
                 min_lift: float = 1.5,
                 max_q: float = 0.05,
                 n_iter: int = 200,
                 seed: int = 20260528) -> dict:
    """
    プラセボ（並べ替え）検定。

    finish結果（各レースの着順）を「選手↔結果」の対応が壊れるようレース間で
    シャッフルし、mine_signs 相当（同じ補正・採用条件）を n_iter 回反復して、
    帰無分布での採用サイン数を集計する。エントリ（誰が何号艇か）は固定し、
    付け替えるのは finish のみ。これで LHS↔RHS の連関を断つ。

    returns: {
        "observed":   実測の採用サイン数,
        "null_mean":  帰無分布の平均採用数,
        "null_p95":   帰無分布の95パーセンタイル,
        "null_max":   帰無分布の最大,
        "empirical_p":帰無で observed 以上が出る経験的p値,
        "n_iter":     反復回数,
    }
    """
    if not races:
        return {"observed": 0, "null_mean": 0.0, "null_p95": 0,
                "null_max": 0, "empirical_p": 1.0, "n_iter": 0}

    # 実測の採用サイン数
    observed = len(mine_signs(
        races, min_support=min_support, min_confidence=min_confidence,
        min_lift=min_lift, max_q=max_q))

    # 有効なレース（top3が3艇確定）の finish だけを抜き出してシャッフル対象にする
    valid_idx = []
    for i, race in enumerate(races):
        top3 = {f[1] for f in race["finish"][:3]}
        top1 = race["finish"][0][1] if race["finish"] else None
        if len(top3) >= 3 and top1 is not None:
            valid_idx.append(i)

    rng = random.Random(seed)
    null_counts: list[int] = []
    finishes = [races[i]["finish"] for i in valid_idx]

    for _ in range(n_iter):
        perm = finishes[:]
        rng.shuffle(perm)
        # finish を付け替えた仮想レース集合（entries は固定）
        shuffled = []
        for j, i in enumerate(valid_idx):
            r = races[i]
            shuffled.append({
                "date": r.get("date"), "jcd": r.get("jcd"), "rno": r.get("rno"),
                "entries": r["entries"],
                "finish": perm[j],
            })
        n_sel = len(mine_signs(
            shuffled, min_support=min_support, min_confidence=min_confidence,
            min_lift=min_lift, max_q=max_q))
        null_counts.append(n_sel)

    null_counts.sort()
    n = len(null_counts)
    null_mean = sum(null_counts) / n if n else 0.0
    null_p95 = null_counts[min(int(0.95 * n), n - 1)] if n else 0
    null_max = null_counts[-1] if n else 0
    ge = sum(1 for x in null_counts if x >= observed)
    empirical_p = (ge + 1) / (n + 1) if n else 1.0

    return {
        "observed": observed,
        "null_mean": round(null_mean, 3),
        "null_p95": null_p95,
        "null_max": null_max,
        "empirical_p": round(empirical_p, 4),
        "n_iter": n_iter,
    }


def save_signs(conn, signs: list[dict]) -> int:
    """signs テーブルを全置換"""
    now = datetime.datetime.now().isoformat()
    conn.execute("DELETE FROM signs")
    for s in signs:
        conn.execute("""
            INSERT INTO signs(toban, racer_name, cond_lane, target_pair, target_kind,
                support, hits, confidence, lift, p_value, q_value, discovered_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (s["toban"], s["racer_name"], s["cond_lane"],
              s["target_pair"], s["target_kind"],
              s["support"], s["hits"], s["confidence"],
              s["lift"], s["p_value"], s.get("q_value"), now))
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


def racer_search(conn, toban: str, *,
                 min_support: int = 3,
                 min_confidence: float = 0.6) -> dict:
    """
    注目選手「大捜索」: ある選手(toban)について、乗った各コース(lane 1-6)ごとに
      - どの艇が1着になりやすいか            (kind='win')
      - どの艇ペアが両方3着以内に来やすいか  (kind='top3_pair')
    を緩い閾値で網羅的に洗い出す。

    個人はデータが少ないので閾値は緩め(support>=3, confidence>=0.6)。
    母比率(lift算出用)は「女子戦・結果ありの全レース」から求める。

    返り値:
      {
        "toban": str, "racer_name": str, "total_runs": int,
        "lane_counts": {lane: 出走数},
        "candidates": [ {toban, racer_name, cond_lane, target_pair, target_kind,
                         support, hits, confidence, lift}, ... ]   # confidence DESC
      }
    """
    # ── 母比率: 女子戦・結果ありの全レースから ─────────────────────
    global_pair: dict[tuple[int, int], int] = defaultdict(int)
    global_win: dict[int, int] = defaultdict(int)
    total_races = 0
    rows = conn.execute("""
        SELECT rr.finish_json
        FROM races r
        JOIN race_results rr ON r.date=rr.date AND r.jcd=rr.jcd AND r.rno=rr.rno
        WHERE r.has_card=1 AND r.has_result=1 AND r.is_ladies=1
    """).fetchall()
    for row in rows:
        fin = json.loads(row["finish_json"])
        top3 = {f[1] for f in fin[:3]}
        top1 = fin[0][1] if fin else None
        if len(top3) < 3 or top1 is None:
            continue
        total_races += 1
        for a in range(1, 7):
            for b in range(a + 1, 7):
                if a in top3 and b in top3:
                    global_pair[(a, b)] += 1
        global_win[top1] += 1
    base_pair = {ab: cnt / total_races for ab, cnt in global_pair.items()} if total_races else {}
    base_win = {m: cnt / total_races for m, cnt in global_win.items()} if total_races else {}

    # ── この選手のレースを取得 ─────────────────────────────────────
    erows = conn.execute("""
        SELECT e.date, e.jcd, e.rno, e.lane, e.name, rr.finish_json
        FROM race_entries e
        JOIN races r ON e.date=r.date AND e.jcd=r.jcd AND e.rno=r.rno
        JOIN race_results rr ON e.date=rr.date AND e.jcd=rr.jcd AND e.rno=rr.rno
        WHERE e.toban=? AND r.is_ladies=1 AND r.has_card=1 AND r.has_result=1
        ORDER BY e.date, e.jcd, e.rno
    """, (toban,)).fetchall()

    racer_name = erows[0]["name"] if erows else ""
    lane_counts: dict[int, int] = defaultdict(int)
    support: dict[int, int] = defaultdict(int)              # lane -> 出走数
    hits_pair: dict[tuple[int, tuple[int, int]], int] = defaultdict(int)  # (lane,(a,b)) -> ヒット
    hits_win: dict[tuple[int, int], int] = defaultdict(int)               # (lane,m) -> ヒット

    for row in erows:
        fin = json.loads(row["finish_json"])
        top3 = {f[1] for f in fin[:3]}
        top1 = fin[0][1] if fin else None
        if len(top3) < 3 or top1 is None:
            continue
        lane = row["lane"]
        lane_counts[lane] += 1
        support[lane] += 1
        for a in range(1, 7):
            for b in range(a + 1, 7):
                if a in top3 and b in top3:
                    hits_pair[(lane, (a, b))] += 1
        hits_win[(lane, top1)] += 1

    candidates: list[dict] = []

    # top3_pair 候補
    for (lane, ab), hits in hits_pair.items():
        sup = support[lane]
        if sup < min_support:
            continue
        conf = hits / sup
        if conf < min_confidence:
            continue
        base = base_pair.get(ab, 1e-9)
        lift = conf / base if base > 0 else float("inf")
        candidates.append({
            "toban": toban,
            "racer_name": racer_name,
            "cond_lane": lane,
            "target_pair": f"{ab[0]}-{ab[1]}",
            "target_kind": "top3_pair",
            "support": sup,
            "hits": hits,
            "confidence": round(conf, 4),
            "lift": round(lift, 3),
        })

    # win 候補
    for (lane, m), hits in hits_win.items():
        sup = support[lane]
        if sup < min_support:
            continue
        conf = hits / sup
        if conf < min_confidence:
            continue
        base = base_win.get(m, 1e-9)
        lift = conf / base if base > 0 else float("inf")
        candidates.append({
            "toban": toban,
            "racer_name": racer_name,
            "cond_lane": lane,
            "target_pair": str(m),
            "target_kind": "win",
            "support": sup,
            "hits": hits,
            "confidence": round(conf, 4),
            "lift": round(lift, 3),
        })

    # ソート: confidence DESC, lift DESC, support DESC
    candidates.sort(key=lambda c: (-c["confidence"], -c["lift"], -c["support"]))

    return {
        "toban": toban,
        "racer_name": racer_name,
        "total_runs": sum(lane_counts.values()),
        "lane_counts": {k: lane_counts[k] for k in sorted(lane_counts)},
        "candidates": candidates,
    }


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
                  f"lift={s['lift']}  p={s['p_value']}  q={s.get('q_value')}")
        if "--save" in sys.argv:
            n = save_signs(conn, signs)
            print(f"DBに{n}件保存しました")
