#!/usr/bin/env python3
"""
オカルト/異常値ウォッチリストの評価ロジック（CLI とサーバー route の共通実装）。

occult_watchlist.json の各候補について、与えられた DB 接続(con)から
  条件(AND) を満たす女子戦レース数(support) と、指定2艇が共に3着以内だった数(hits)
を再計算し、的中率(rate) と そのペアの全体ベース率に対する倍率(lift) を出す。

判定:
  dim=="異常lift" の候補 … lift基準 (生存>=2.0倍 / 黄>=1.5倍 / それ未満は淘汰)
  それ以外            … 的中率基準 (生存>=survive_rate / 黄>=watch_rate)

import しても副作用なし（評価は evaluate(con) 呼び出し時のみ）。READ-ONLY 前提。
"""
from __future__ import annotations
import json
import os
from itertools import combinations

BASE = os.path.dirname(os.path.abspath(__file__))
WL_PATH = os.path.join(BASE, "occult_watchlist.json")


def load_watchlist() -> dict:
    with open(WL_PATH, encoding="utf-8") as f:
        return json.load(f)


def _jun(day: int) -> str:
    return "上旬" if day <= 10 else ("中旬" if day <= 20 else "下旬")


def _load_races(con):
    res = {}
    for r in con.execute("SELECT date, jcd, rno, finish_json FROM race_results"):
        f = json.loads(r["finish_json"])
        if len(f) >= 3:
            res[(r["date"], r["jcd"], r["rno"])] = set(x[1] for x in f[:3])
    races = {}
    for r in con.execute(
        """SELECT e.date, e.jcd, e.rno, e.lane, e.name, e.toban, e.motor_no, r.venue
           FROM race_entries e JOIN races r ON e.date=r.date AND e.jcd=r.jcd AND e.rno=r.rno
           WHERE r.is_ladies=1 AND r.has_result=1"""):
        k = (r["date"], r["jcd"], r["rno"])
        if k not in res:
            continue
        info = races.setdefault(k, {"lanes": {}, "names": set(), "venue": r["venue"],
                                    "rno": r["rno"], "date": r["date"]})
        nm = "".join((r["name"] or "").split()).replace("　", "")
        info["lanes"][r["lane"]] = {"name": nm, "toban": r["toban"], "motor": r["motor_no"]}
        info["names"].add(nm)
    return races, res


def matches(info, preds) -> bool:
    for p in preds:
        t = p["t"]
        lane = info["lanes"].get(p.get("lane"), {})
        if t == "racer":
            if lane.get("name") != p["name"]:
                return False
        elif t == "racer_present":
            if p["name"] not in info["names"]:
                return False
        elif t == "venue":
            if info["venue"] != p["venue"]:
                return False
        elif t == "rno":
            if info["rno"] != p["rno"]:
                return False
        elif t == "motor_last":
            m = lane.get("motor")
            if m is None or int(str(m)[-1]) != p["digit"]:
                return False
        elif t == "toban_last":
            tb = lane.get("toban")
            if not tb or str(tb)[-1] != str(p["digit"]):
                return False
        elif t == "name_kanji":
            if p["kanji"] not in (lane.get("name") or ""):
                return False
        elif t == "month":
            if int(info["date"][4:6]) != p["month"]:
                return False
        elif t == "dom_div":
            if int(info["date"][6:8]) % p["div"] != 0:
                return False
        elif t == "jun":
            if _jun(int(info["date"][6:8])) != p["jun"]:
                return False
        else:
            return False
    return True


def cond_desc(preds) -> str:
    out = []
    for p in preds:
        t = p["t"]
        if t == "racer":
            out.append(f"{p['name']}@{p['lane']}号")
        elif t == "racer_present":
            out.append(f"{p['name']}出走")
        elif t == "venue":
            out.append(p["venue"])
        elif t == "rno":
            out.append(f"{p['rno']}R")
        elif t == "motor_last":
            out.append(f"M末尾{p['digit']}@{p['lane']}号")
        elif t == "toban_last":
            out.append(f"登番末尾{p['digit']}@{p['lane']}号")
        elif t == "name_kanji":
            out.append(f"名前『{p['kanji']}』@{p['lane']}号")
        elif t == "month":
            out.append(f"{p['month']}月")
        elif t == "dom_div":
            out.append(f"{p['div']}の倍数日")
        elif t == "jun":
            out.append(p["jun"])
    return " & ".join(out)


def evaluate(con) -> dict:
    """con(row_factory=Row) を使ってウォッチリスト全候補を評価し、結果dictを返す。"""
    wl = load_watchlist()
    surv = wl["criteria"]["survive_rate"]
    watch = wl["criteria"]["watch_rate"]
    races, res = _load_races(con)
    tot = len(races)

    # 全ペアのベース率（lift算出用）
    pairbase = {}
    for a, b in combinations(range(1, 7), 2):
        c = sum(1 for k in races if a in res[k] and b in res[k])
        pairbase[(a, b)] = c / tot if tot else 0.0

    rows = []
    for cand in sorted(wl["candidates"], key=lambda x: x["id"]):
        a, b = cand["pair"]
        sup = hit = 0
        for k, info in races.items():
            if matches(info, cand["preds"]):
                sup += 1
                if a in res[k] and b in res[k]:
                    hit += 1
        rate = hit / sup if sup else 0.0
        pb = pairbase.get(tuple(sorted((a, b))), 0.0)
        lift = rate / pb if pb > 0 else 0.0
        if cand.get("dim") in ("異常lift", "ROI実証"):
            verdict = "生存" if lift >= 2.0 else ("黄信号" if lift >= 1.5 else "淘汰")
        else:
            verdict = "生存" if rate >= surv else ("黄信号" if rate >= watch else "淘汰")
        base = cand["base"]
        rows.append({
            "id": cand["id"],
            "dim": cand.get("dim", ""),
            "pair": [a, b],
            "desc": cond_desc(cand["preds"]),
            "base_hits": base["hits"],
            "base_support": base["support"],
            "base_rate": (base["hits"] / base["support"]) if base["support"] else 0.0,
            "support": sup,
            "hits": hit,
            "rate": rate,
            "lift": lift,
            "growth": sup - base["support"],
            "verdict": verdict,
        })
    return {
        "recorded_at": wl["recorded_at"],
        "note": wl.get("note", ""),
        "total_races": tot,
        "survive_rate": surv,
        "watch_rate": watch,
        "rows": rows,
    }
