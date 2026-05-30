#!/usr/bin/env python3
"""複合予想エンジン（ランタイム・READ-ONLY）。

composite_model.json の重みを読み、1レースの出走表(entries)から各艇の
『3着以内(top3)確率』と『異常値(枠ベースからの乖離)』を算出して予想の軸を組む。

異常値 anomaly = P(top3) − 枠ベース率。
  大きな正 … 枠の評価以上に強い艇＝過小評価＝狙い目/穴の軸
  大きな負 … 枠の評価ほど信用できない艇＝危険/消し候補

オカルト・ウォッチリスト(occult_watchlist.json)の発火サインも重ねて表示する
（複合判断）。学習はしない＝サーバ高速。重み更新は build_model.py を再実行。
"""
from __future__ import annotations
import json
import math
import os

import watchlist_eval

BASE = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(BASE, "composite_model.json")

_MODEL = None


def load_model() -> dict | None:
    global _MODEL
    if _MODEL is None:
        if not os.path.exists(MODEL_PATH):
            return None
        with open(MODEL_PATH, encoding="utf-8") as f:
            _MODEL = json.load(f)
    return _MODEL


def _sigmoid(z: float) -> float:
    return 1 / (1 + math.exp(-max(min(z, 30), -30)))


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def score_entry(m: dict, e) -> float:
    """1艇のtop3確率。e は dict 或いは sqlite Row（lane, 各能力値, flying を持つ）。"""
    w = m["weights"]
    lane = int(e["lane"])
    z = m["bias"] + w.get(f"lane{lane}", 0.0)
    for f in m["num_feats"]:
        mu, sd = m["stats"][f]
        v = _num(e[f] if f in e.keys() else None) if hasattr(e, "keys") else _num(e.get(f))
        if v is None:
            v = mu
        z += w.get(f, 0.0) * ((v - mu) / sd)
    fly = _num(e["flying"] if hasattr(e, "keys") and "flying" in e.keys() else
               (e.get("flying") if hasattr(e, "get") else None)) or 0.0
    z += w.get("flying", 0.0) * (1.0 if fly > 0 else 0.0)
    return _sigmoid(z)


def _g(e, key):
    """dict / sqlite Row 両対応の取得。"""
    if hasattr(e, "keys"):
        return e[key] if key in e.keys() else None
    return e.get(key)


def _build_info(entries, race_meta):
    """watchlist_eval.matches 用の info dict を出走表から組む。"""
    lanes = {}
    names = set()
    for e in entries:
        nm = "".join(str(_g(e, "name") or "").split()).replace("　", "")
        lane = int(_g(e, "lane"))
        lanes[lane] = {"name": nm, "toban": _g(e, "toban"), "motor": _g(e, "motor_no")}
        if nm:
            names.add(nm)
    date = (race_meta or {}).get("date", "")
    return {
        "lanes": lanes, "names": names,
        "venue": (race_meta or {}).get("venue", ""),
        "rno": (race_meta or {}).get("rno", 0),
        "date": date,
    }


def _occult_fires(entries, race_meta):
    """このレースで発火するオカルト・ウォッチリスト候補を返す（複合判断の材料）。"""
    if not race_meta:
        return []
    try:
        wl = watchlist_eval.load_watchlist()
    except Exception:
        return []
    info = _build_info(entries, race_meta)
    out = []
    for cand in wl.get("candidates", []):
        try:
            if watchlist_eval.matches(info, cand["preds"]):
                base = cand.get("base", {})
                sup = base.get("support", 0)
                hits = base.get("hits", 0)
                out.append({
                    "id": cand["id"],
                    "dim": cand.get("dim", ""),
                    "pair": cand["pair"],
                    "desc": watchlist_eval.cond_desc(cand["preds"]),
                    "base_rate": (hits / sup) if sup else 0.0,
                    "base": f"{hits}/{sup}",
                })
        except Exception:
            continue
    out.sort(key=lambda x: -x["base_rate"])
    return out


MARK = ["◎", "○", "▲", "△", "×", "×"]      # 本命/対抗/相手/相手/...


def predict_race(entries, race_meta=None) -> dict:
    """出走表(entries: 6艇)から複合予想を返す。"""
    m = load_model()
    if m is None:
        return {"ok": False, "error": "model not found (run build_model.py)"}
    ents = [e for e in entries if _g(e, "lane")]
    if len(ents) < 4:
        return {"ok": False, "error": "出走表が不足しています"}

    boats = []
    for e in ents:
        lane = int(_g(e, "lane"))
        p = score_entry(m, e)
        base = m["lane_base"].get(str(lane), 0.5)
        boats.append({
            "lane": lane,
            "name": _g(e, "name") or "",
            "rank": _g(e, "rank") or "",
            "toban": _g(e, "toban") or "",
            "p": round(p, 4),
            "base": round(base, 4),
            "anomaly": round(p - base, 4),
        })
    boats.sort(key=lambda x: -x["p"])

    # ラベル付与（順位 + 異常値）
    for i, bt in enumerate(boats):
        label = MARK[i] if i < len(MARK) else "×"
        flags = []
        if bt["anomaly"] >= 0.08 and bt["lane"] >= 4:
            flags.append("妙味")          # 枠より強い・過小評価
        if bt["anomaly"] <= -0.10:
            flags.append("危険")          # 枠より弱い
        bt["mark"] = label
        bt["flags"] = flags

    lanes_sorted = [b["lane"] for b in boats]
    axis = lanes_sorted[:2]
    partners = lanes_sorted[2:4]

    # 最大の正/負異常値（1号以外を主対象に）
    pos = max((b for b in boats if b["lane"] != 1), key=lambda x: x["anomaly"], default=None)
    neg = min(boats, key=lambda x: x["anomaly"])
    anomaly_pos = None
    if pos and pos["anomaly"] >= 0.05:
        anomaly_pos = {"lane": pos["lane"], "name": pos["name"], "anomaly": pos["anomaly"],
                       "text": f"{pos['lane']}号 {pos['name']} は枠の評価({pos['base']*100:.0f}%)以上に強い（+{pos['anomaly']*100:.0f}pt）＝過小評価の軸候補"}
    anomaly_neg = None
    if neg and neg["anomaly"] <= -0.08:
        anomaly_neg = {"lane": neg["lane"], "name": neg["name"], "anomaly": neg["anomaly"],
                       "text": f"{neg['lane']}号 {neg['name']} は枠の評価({neg['base']*100:.0f}%)ほど信用できない（{neg['anomaly']*100:.0f}pt）＝消し候補"}

    # 参考フォーメーション（ROI保証ではなく軸の提示）
    top4 = lanes_sorted[:4]
    a = lanes_sorted[0]
    box4 = ["-".join(str(x) for x in sorted(c))
            for c in _combos(top4, 3)]
    nagashi = ["-".join(str(x) for x in sorted([a, *c]))
               for c in _combos(lanes_sorted[1:4], 2)]
    tickets = [
        {"type": "3連複", "name": "軸1頭流し", "axis": [a],
         "combos": nagashi, "note": f"{a}号を軸に上位相手3艇 ({len(nagashi)}点)"},
        {"type": "3連複", "name": "上位4ボックス", "combos": box4,
         "note": f"上位4艇のボックス ({len(box4)}点)"},
    ]

    return {
        "ok": True,
        "boats": boats,
        "axis": axis,
        "partners": partners,
        "anomaly_pos": anomaly_pos,
        "anomaly_neg": anomaly_neg,
        "tickets": tickets,
        "occult_fires": _occult_fires(ents, race_meta),
        "model": {
            "trained_at": m.get("trained_at", ""),
            "n_races": m.get("n_races", 0),
            "metrics": m.get("metrics", {}),
        },
    }


def _combos(seq, r):
    from itertools import combinations
    return list(combinations(seq, r))
