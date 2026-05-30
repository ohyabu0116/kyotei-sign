#!/usr/bin/env python3
"""複合予想エンジンのモデル学習器（オフライン実行・READ-ONLY）。

女子戦の per-boat『3着以内(top3)』確率を、枠(lane)+選手力(全国勝率/3連率/当地)+
スタート(avg_st)+モーター+体重 から純Pythonのロジスティック回帰で学習し、
composite_model.json に重みを書き出す。サーバはこのJSONを読むだけ（学習しない=高速）。

データ拡充時に  python3 build_model.py  を再実行して重みを更新→コミットする。
相関の強い勝率系は勾配降下が自動で減衰させる（Naive Bayesの二重計上を回避）。
"""
import sqlite3, json, os, math, random, datetime
from collections import defaultdict

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "kyotei_sign.db")
OUT = os.path.join(BASE, "composite_model.json")

NUM = ["national_win", "national_3", "local_win", "motor_3", "avg_st", "weight", "age"]
LANES = [1, 2, 3, 4, 5, 6]


def load_rows():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    top3 = {}
    for r in con.execute("SELECT date,jcd,rno,finish_json FROM race_results"):
        f = json.loads(r["finish_json"])
        if len(f) >= 3:
            top3[(r["date"], r["jcd"], r["rno"])] = set(x[1] for x in f[:3])
    rows = []
    for r in con.execute(
        """SELECT e.* FROM race_entries e
           JOIN races r2 ON e.date=r2.date AND e.jcd=r2.jcd AND e.rno=r2.rno
           WHERE r2.is_ladies=1 AND r2.has_result=1"""):
        k = (r["date"], r["jcd"], r["rno"])
        if k not in top3:
            continue
        d = dict(r)
        d["_k"] = k
        d["_y"] = 1 if r["lane"] in top3[k] else 0
        rows.append(d)
    con.close()
    return rows


def fit_stats(rows):
    stats = {}
    for f in NUM:
        vals = [r[f] for r in rows if r.get(f) is not None]
        mu = sum(vals) / len(vals)
        sd = (sum((v - mu) ** 2 for v in vals) / len(vals)) ** 0.5 or 1.0
        stats[f] = (mu, sd)
    return stats


def featvec(r, stats):
    x = [0.0] * 6
    x[r["lane"] - 1] = 1.0
    for f in NUM:
        mu, sd = stats[f]
        v = r.get(f)
        v = mu if v is None else v
        x.append((v - mu) / sd)
    x.append(1.0 if (r.get("flying") or 0) > 0 else 0.0)
    return x


NF = 6 + len(NUM) + 1
NAMES = [f"lane{L}" for L in LANES] + NUM + ["flying"]


def sigmoid(z):
    return 1 / (1 + math.exp(-max(min(z, 30), -30)))


def train(rows, stats, l2=1.0, lr=0.3, epochs=250, seed=42):
    random.seed(seed)
    w = [0.0] * NF
    b = 0.0
    X = [featvec(r, stats) for r in rows]
    Y = [r["_y"] for r in rows]
    n = len(rows)
    idx = list(range(n))
    for _ in range(epochs):
        random.shuffle(idx)
        gw = [0.0] * NF
        gb = 0.0
        for i in idx:
            z = b + sum(w[j] * X[i][j] for j in range(NF))
            e = sigmoid(z) - Y[i]
            for j in range(NF):
                if X[i][j] != 0.0:
                    gw[j] += e * X[i][j]
            gb += e
        for j in range(NF):
            w[j] -= lr * (gw[j] / n + l2 / n * w[j])
        b -= lr * (gb / n)
    return w, b


def predict(r, w, b, stats):
    fv = featvec(r, stats)
    return sigmoid(b + sum(w[j] * fv[j] for j in range(NF)))


def oos_metrics(rows):
    """70/30時系列分割でOOS指標を測る（正直さのためのメタdata）。"""
    dates = sorted(set(r["date"] for r in rows))
    cut = dates[int(len(dates) * 0.70)]
    tr = [r for r in rows if r["date"] < cut]
    te = [r for r in rows if r["date"] >= cut]
    st = fit_stats(tr)
    w, b = train(tr, st)
    lane_h = defaultdict(int); lane_n = defaultdict(int)
    for r in tr:
        lane_n[r["lane"]] += 1; lane_h[r["lane"]] += r["_y"]
    p_lane = {L: lane_h[L] / lane_n[L] for L in lane_n}

    def ll(data, fn):
        s = 0.0
        for r in data:
            p = min(max(fn(r), 1e-9), 1 - 1e-9)
            s += -(r["_y"] * math.log(p) + (1 - r["_y"]) * math.log(1 - p))
        return s / len(data)

    m_ll = ll(te, lambda r: predict(r, w, b, st))
    l_ll = ll(te, lambda r: p_lane[r["lane"]])
    # AUC
    sc = sorted((predict(r, w, b, st), r["_y"]) for r in te)
    pos = sum(y for _, y in sc); neg = len(sc) - pos
    rank = 0.0; i = 0
    while i < len(sc):
        j = i
        while j < len(sc) and sc[j][0] == sc[i][0]:
            j += 1
        avg = (i + j - 1) / 2 + 1
        for k in range(i, j):
            if sc[k][1] == 1:
                rank += avg
        i = j
    auc = (rank - pos * (pos + 1) / 2) / (pos * neg)
    # axis power
    byrace = defaultdict(list)
    for r in te:
        byrace[r["_k"]].append(r)
    m1 = l1 = m2 = l2c = tot = 0
    for k, ents in byrace.items():
        if len(ents) < 6:
            continue
        tot += 1
        ms = sorted(ents, key=lambda r: -predict(r, w, b, st))
        ls = sorted(ents, key=lambda r: -p_lane[r["lane"]])
        if ms[0]["_y"]: m1 += 1
        if ls[0]["_y"]: l1 += 1
        if sum(e["_y"] for e in ms[:2]) == 2: m2 += 1
        if sum(e["_y"] for e in ls[:2]) == 2: l2c += 1
    return {
        "oos_logloss": round(m_ll, 4), "lane_logloss": round(l_ll, 4),
        "auc": round(auc, 4),
        "axis_top1_top3": round(m1 / tot, 4), "lane_top1_top3": round(l1 / tot, 4),
        "axis_top2_both_top3": round(m2 / tot, 4), "lane_top2_both_top3": round(l2c / tot, 4),
        "n_test_races": tot, "cut_date": cut,
        "note_3rempuku_roi": "flat 3連複は約85%(=控除率)。本モデルは確率/軸の精度向上が役割でROI保証ではない。",
    }


def main():
    rows = load_rows()
    n_races = len(set(r["_k"] for r in rows))
    print(f"学習データ: {len(rows)} エントリ / {n_races} レース")

    lane_h = defaultdict(int); lane_n = defaultdict(int)
    for r in rows:
        lane_n[r["lane"]] += 1; lane_h[r["lane"]] += r["_y"]
    lane_base = {str(L): lane_h[L] / lane_n[L] for L in LANES}

    print("OOS指標を測定中（70/30時系列分割）...")
    metrics = oos_metrics(rows)
    print(f"  OOS logloss モデル={metrics['oos_logloss']} / 枠のみ={metrics['lane_logloss']}  AUC={metrics['auc']}")
    print(f"  軸 上位2両top3 モデル={metrics['axis_top2_both_top3']*100:.1f}% / 枠順={metrics['lane_top2_both_top3']*100:.1f}%")

    print("最終モデルを全データで学習中...")
    stats = fit_stats(rows)
    w, b = train(rows, stats)

    model = {
        "model_version": "1.0",
        "trained_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "n_races": n_races, "n_entries": len(rows),
        "lane_base": {k: round(v, 4) for k, v in lane_base.items()},
        "num_feats": NUM,
        "stats": {f: [round(stats[f][0], 4), round(stats[f][1], 4)] for f in NUM},
        "weights": {NAMES[j]: round(w[j], 4) for j in range(NF)},
        "bias": round(b, 4),
        "metrics": metrics,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(model, f, ensure_ascii=False, indent=2)
    print(f"→ {OUT} に書き出し完了")
    # weight summary
    ws = sorted(model["weights"].items(), key=lambda x: -abs(x[1]))
    print("重み(絶対値順):")
    for nm, wt in ws:
        print(f"  {nm:14s} {wt:+.3f}")


if __name__ == "__main__":
    main()
