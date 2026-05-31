#!/usr/bin/env python3
"""
荒れエンジン(オカルト専用): 競艇 女子戦(is_ladies=1)で「この条件が整うと、なぜか荒れる」
というオカルト的な共起条件を炙り出す。荒れ = 3連単払戻(payout_3t_yen)が高いこと。

  やや荒れ(soft) : payout >= ¥5,000
  万舟(manshu)   : payout >= ¥10,000

統計的妥当性(p値/FDR/アウトオブサンプル)は一切無視。READ-ONLY(mode=ro)。
miner.py / manshu_hunt.py 等は import せず、述語ボキャブラリだけを流用した独立エンジン。

注目アウトプット:
  - arashi_lane3 : 3号艇に座ると荒れる「荒らし役」候補
  - are_regulars : 出走するだけで万舟率が跳ねる「荒れ常連」
  - rough_venues : 荒れ水面(浜名湖・平和島 ほか)
  - mine_are_conditions : 汎用の条件マイナー(depth1+depth2 AND)
  - common_factors : lift上位条件に共通する要素の抽出
"""
import sqlite3
import os
import statistics
from itertools import combinations
from collections import defaultdict, Counter

SOFT_YEN = 5000
MANSHU_YEN = 10000

KANJI = ("子", "美", "菜", "奈", "花", "あ", "ゆ")


def jun_of(d):
    return "上旬" if d <= 10 else ("中旬" if d <= 20 else "下旬")


def _clean_name(s):
    return "".join((s or "").split()).replace("　", "")


def tier(payout_3t_yen):
    """None or <5000 -> 0 ; 5000..9999 -> 1 ; >=10000 -> 2"""
    if payout_3t_yen is None:
        return 0
    if payout_3t_yen >= MANSHU_YEN:
        return 2
    if payout_3t_yen >= SOFT_YEN:
        return 1
    return 0


def load_races(conn):
    """key (date,jcd,rno) -> info dict.

    info = {"date","jcd","rno":int,"venue","title",
            "lanes": {lane:int -> {"name","toban","motor":int|None}},
            "payout":int|None, "tier":int, "finish_top3": set[int],
            "finish_order": list[int]}  # 1着→2着→… の号艇(3連単用)
    """
    import json
    conn.row_factory = sqlite3.Row
    # 結果(払戻・着順)
    res = {}
    for r in conn.execute(
        "SELECT date,jcd,rno,payout_3t_yen,finish_json FROM race_results"
    ):
        k = (r["date"], r["jcd"], r["rno"])
        top3 = set()
        order = []
        fj = r["finish_json"]
        if fj:
            try:
                f = json.loads(fj)
                f_sorted = sorted(f, key=lambda x: int(x[0]))  # rank昇順
                order = [int(x[1]) for x in f_sorted]
                top3 = set(order[:3])
            except Exception:
                top3 = set()
                order = []
        res[k] = (r["payout_3t_yen"], top3, order)

    races = {}
    for r in conn.execute(
        """SELECT e.date,e.jcd,e.rno,e.lane,e.toban,e.name,e.motor_no,
                  r2.venue,r2.title
           FROM race_entries e
           JOIN races r2 ON e.date=r2.date AND e.jcd=r2.jcd AND e.rno=r2.rno
           WHERE r2.is_ladies=1 AND r2.has_result=1"""
    ):
        k = (r["date"], r["jcd"], r["rno"])
        if k not in res:
            continue
        info = races.get(k)
        if info is None:
            payout, top3, order = res[k]
            info = {
                "date": r["date"],
                "jcd": r["jcd"],
                "rno": int(r["rno"]),
                "venue": r["venue"],
                "title": r["title"],
                "lanes": {},
                "payout": payout,
                "tier": tier(payout),
                "finish_top3": top3,
                "finish_order": order,
            }
            races[k] = info
        m = r["motor_no"]
        info["lanes"][int(r["lane"])] = {
            "name": _clean_name(r["name"]),
            "toban": r["toban"],
            "motor": int(m) if m is not None else None,
        }
    return races


def preds_of(info):
    """info(lanes/venue/rno/date) から述語タプルのリストを生成。"""
    out = []
    for lane, ed in info["lanes"].items():
        nm = ed.get("name") or ""
        if nm:
            out.append(("R", nm, lane))
            out.append(("RP", nm))
            if lane == 3:
                out.append(("A3", nm))  # 3号艇 = 荒らし役候補
            for ka in KANJI:
                if ka in nm:
                    out.append(("K", ka, lane))
        m = ed.get("motor")
        if m is not None:
            out.append(("ML", int(str(m)[-1]), lane))
        tb = ed.get("toban")
        if tb:
            out.append(("TL", int(str(tb)[-1]), lane))
    out.append(("V", info["venue"]))
    out.append(("N", info["rno"]))
    out.append(("M", int(info["date"][4:6])))
    d = int(info["date"][6:8])
    out.append(("J", jun_of(d)))
    if d % 3 == 0:
        out.append(("D3",))
    return out


def redundant(p1, p2):
    """両方が R/RP で同じ選手名なら True(depth-2 で重複扱い)。"""
    if p1[0] in ("R", "RP") and p2[0] in ("R", "RP"):
        return p1[1] == p2[1]
    return False


def cond_desc(cond):
    """述語タプル列を日本語へ。"""
    out = []
    for p in cond:
        t = p[0]
        if t == "A3":
            out.append(f"{p[1]}が3号艇(荒らし役)")
        elif t == "R":
            out.append(f"{p[1]}@{p[2]}号")
        elif t == "RP":
            out.append(f"{p[1]}出走")
        elif t == "V":
            out.append(p[1])
        elif t == "N":
            out.append(f"{p[1]}R")
        elif t == "M":
            out.append(f"{p[1]}月")
        elif t == "J":
            out.append(p[1])
        elif t == "D3":
            out.append("3の倍数日")
        elif t == "ML":
            out.append(f"M末尾{p[1]}@{p[2]}号")
        elif t == "TL":
            out.append(f"登番末尾{p[1]}@{p[2]}号")
        elif t == "K":
            out.append(f"『{p[1]}』@{p[2]}号")
    return " & ".join(out)


def base_rates(races):
    """全体ベース率。"""
    n = len(races)
    if n == 0:
        return {"manshu_rate": 0.0, "soft_rate": 0.0, "n": 0, "avg_pay_manshu": 0.0}
    mans = sum(1 for i in races.values() if i["tier"] >= 2)
    soft = sum(1 for i in races.values() if i["tier"] >= 1)
    mpays = [i["payout"] for i in races.values()
             if i["tier"] >= 2 and i["payout"] is not None]
    return {
        "manshu_rate": mans / n,
        "soft_rate": soft / n,
        "n": n,
        "avg_pay_manshu": statistics.mean(mpays) if mpays else 0.0,
    }


def mine_are_conditions(races, *, min_support=15, target_tier=2):
    """depth1 + depth2 AND(redundant はスキップ)で荒れ条件をマイニング。

    各 cond(support>=min_support)について:
      cond,desc,support,hits(=target_tier以上の数),conf,lift(=conf/baseのtarget率),
      soft_hits,soft_rate,avg_pay_all,avg_pay_manshu,expect_index,types
    lift 降順で qualifying 全件を返す(呼び出し側でスライス)。
    """
    n = len(races)
    if n == 0:
        return []
    base_hit = sum(1 for i in races.values() if i["tier"] >= target_tier)
    base = base_hit / n if n else 0.0

    sup = defaultdict(int)
    hit = defaultdict(int)        # tier >= target_tier
    soft = defaultdict(int)       # tier >= 1
    paysum_all = defaultdict(int)   # 払戻ありレースの合計
    paycnt_all = defaultdict(int)   # 払戻ありレース数
    paysum_man = defaultdict(int)   # tier>=2 の払戻合計
    paycnt_man = defaultdict(int)   # tier>=2 のレース数

    for info in races.values():
        plist = preds_of(info)
        is_hit = 1 if info["tier"] >= target_tier else 0
        is_soft = 1 if info["tier"] >= 1 else 0
        is_man = 1 if info["tier"] >= 2 else 0
        pay = info["payout"]
        has_pay = pay is not None
        cands = [(p,) for p in plist]
        for p1, p2 in combinations(plist, 2):
            if redundant(p1, p2):
                continue
            cands.append((p1, p2) if p1 < p2 else (p2, p1))
        for c in cands:
            sup[c] += 1
            hit[c] += is_hit
            soft[c] += is_soft
            if has_pay:
                paysum_all[c] += pay
                paycnt_all[c] += 1
                if is_man:
                    paysum_man[c] += pay
                    paycnt_man[c] += 1

    out = []
    for c, s in sup.items():
        if s < min_support:
            continue
        h = hit[c]
        conf = h / s
        lift = conf / base if base > 0 else 0.0
        sh = soft[c]
        avg_all = paysum_all[c] / paycnt_all[c] if paycnt_all[c] else 0.0
        avg_man = paysum_man[c] / paycnt_man[c] if paycnt_man[c] else 0.0
        types = sorted(set(p[0] for p in c))
        out.append({
            "cond": c,
            "desc": cond_desc(c),
            "support": s,
            "hits": h,
            "conf": conf,
            "lift": lift,
            "soft_hits": sh,
            "soft_rate": sh / s,
            "avg_pay_all": avg_all,
            "avg_pay_manshu": avg_man,
            "expect_index": conf * avg_man,
            "types": types,
        })
    out.sort(key=lambda x: -x["lift"])
    return out


def arashi_lane3(races, *, min_support=8):
    """3号艇に座った各選手の荒れ寄与(万舟基準)。"""
    n = len(races)
    base_hit = sum(1 for i in races.values() if i["tier"] >= 2)
    base = base_hit / n if n else 0.0

    sup = defaultdict(int)
    hit = defaultdict(int)
    paysum_man = defaultdict(int)
    paycnt_man = defaultdict(int)
    for info in races.values():
        ed = info["lanes"].get(3)
        if not ed:
            continue
        nm = ed.get("name") or ""
        if not nm:
            continue
        sup[nm] += 1
        is_man = info["tier"] >= 2
        if is_man:
            hit[nm] += 1
            if info["payout"] is not None:
                paysum_man[nm] += info["payout"]
                paycnt_man[nm] += 1

    out = []
    for nm, s in sup.items():
        if s < min_support:
            continue
        h = hit[nm]
        conf = h / s
        lift = conf / base if base > 0 else 0.0
        avg_man = paysum_man[nm] / paycnt_man[nm] if paycnt_man[nm] else 0.0
        out.append({
            "name": nm,
            "support": s,
            "hits": h,
            "conf": conf,
            "lift": lift,
            "avg_pay_manshu": avg_man,
        })
    out.sort(key=lambda x: (-x["lift"], -x["support"]))
    return out


def are_regulars(races, *, min_support=15):
    """出走するだけで荒れる(万舟率が跳ねる)「荒れ常連」。"""
    n = len(races)
    base_hit = sum(1 for i in races.values() if i["tier"] >= 2)
    base = base_hit / n if n else 0.0

    sup = defaultdict(int)
    hit = defaultdict(int)
    soft = defaultdict(int)
    paysum_man = defaultdict(int)
    paycnt_man = defaultdict(int)
    for info in races.values():
        is_man = info["tier"] >= 2
        is_soft = info["tier"] >= 1
        pay = info["payout"]
        names = set()
        for ed in info["lanes"].values():
            nm = ed.get("name") or ""
            if nm:
                names.add(nm)
        for nm in names:
            sup[nm] += 1
            if is_soft:
                soft[nm] += 1
            if is_man:
                hit[nm] += 1
                if pay is not None:
                    paysum_man[nm] += pay
                    paycnt_man[nm] += 1

    out = []
    for nm, s in sup.items():
        if s < min_support:
            continue
        h = hit[nm]
        conf = h / s
        lift = conf / base if base > 0 else 0.0
        avg_man = paysum_man[nm] / paycnt_man[nm] if paycnt_man[nm] else 0.0
        out.append({
            "name": nm,
            "support": s,
            "hits": h,
            "conf": conf,
            "lift": lift,
            "soft_rate": soft[nm] / s,
            "avg_pay_manshu": avg_man,
        })
    out.sort(key=lambda x: -x["lift"])
    return out


def mine_lane_top3(races, *, min_support=12):
    """号艇ごとのオカルト述語 → 「その号艇が3着以内に入る」 lift をマイニング。

    買い目(3連単)を組むための“どの艇が来るか”シグナル。lane依存述語のみ:
      R(選手@号) / TL(登番末尾@号) / ML(M末尾@号) / K(漢字@号)。
    A3 と RP は lane非依存 or R(@3) と重複するため除外。

    返り値:
      {"base": {lane:int -> 3着内率}, "signals": {pred_tuple -> {...}}}
    lift = P(lane∈top3 | pred) / P(lane∈top3)。基礎率はあくまで内部の正規化分母で、
    「枠の強さ」として表に出すためのものではない。
    """
    lane_cnt = defaultdict(int)
    lane_in = defaultdict(int)
    for info in races.values():
        top3 = info.get("finish_top3") or set()
        if not top3:
            continue
        for lane in info["lanes"].keys():
            lane_cnt[lane] += 1
            if lane in top3:
                lane_in[lane] += 1
    base = {L: (lane_in[L] / lane_cnt[L] if lane_cnt[L] else 0.0)
            for L in lane_cnt}

    sup = defaultdict(int)
    hit = defaultdict(int)
    pred_lane = {}
    for info in races.values():
        top3 = info.get("finish_top3") or set()
        if not top3:
            continue
        for p in preds_of(info):
            t = p[0]
            if t in ("R", "TL", "ML", "K"):
                lane = p[2]
            else:
                continue  # A3/RP/V/N/M/J/D3 は lane非依存 or 重複
            sup[p] += 1
            pred_lane[p] = lane
            if lane in top3:
                hit[p] += 1

    signals = {}
    for p, s in sup.items():
        if s < min_support:
            continue
        lane = pred_lane[p]
        b = base.get(lane, 0.0)
        conf = hit[p] / s
        lift = (conf / b) if b > 0 else 0.0
        signals[p] = {
            "lane": lane,
            "support": s,
            "hits": hit[p],
            "conf": conf,
            "lift": lift,
            "desc": cond_desc((p,)),
        }
    return {"base": base, "signals": signals}


def rough_venues(races):
    """会場別の荒れ度(万舟率)。conf 降順。"""
    n = len(races)
    base_hit = sum(1 for i in races.values() if i["tier"] >= 2)
    base = base_hit / n if n else 0.0

    sup = defaultdict(int)
    hit = defaultdict(int)
    soft = defaultdict(int)
    paysum_man = defaultdict(int)
    paycnt_man = defaultdict(int)
    for info in races.values():
        v = info["venue"]
        if not v:
            continue
        sup[v] += 1
        if info["tier"] >= 1:
            soft[v] += 1
        if info["tier"] >= 2:
            hit[v] += 1
            if info["payout"] is not None:
                paysum_man[v] += info["payout"]
                paycnt_man[v] += 1

    out = []
    for v, s in sup.items():
        h = hit[v]
        conf = h / s
        lift = conf / base if base > 0 else 0.0
        avg_man = paysum_man[v] / paycnt_man[v] if paycnt_man[v] else 0.0
        out.append({
            "venue": v,
            "support": s,
            "hits": h,
            "conf": conf,
            "soft_rate": soft[v] / s,
            "lift": lift,
            "avg_pay_manshu": avg_man,
        })
    out.sort(key=lambda x: -x["conf"])
    return out


def common_factors(conditions, top_n=80):
    """lift上位 top_n 条件から繰り返し登場する要素を集計。label->list[[key,count]]"""
    pool = sorted(conditions, key=lambda x: -x["lift"])[:top_n]
    racer = Counter()
    racer_lane = Counter()
    venue = Counter()
    lane = Counter()
    motor_last = Counter()
    toban_last = Counter()
    kanji = Counter()
    types = Counter()
    for d in pool:
        for p in d["cond"]:
            t = p[0]
            types[t] += 1
            if t == "R":
                racer[p[1]] += 1
                racer_lane[f"{p[1]}@{p[2]}号"] += 1
                lane[f"{p[2]}号"] += 1
            elif t == "RP":
                racer[p[1]] += 1
            elif t == "A3":
                racer[p[1]] += 1
                racer_lane[f"{p[1]}@3号"] += 1
                lane["3号"] += 1
            elif t == "V":
                venue[p[1]] += 1
            elif t == "ML":
                motor_last[f"末尾{p[1]}@{p[2]}号"] += 1
            elif t == "TL":
                toban_last[f"末尾{p[1]}@{p[2]}号"] += 1
            elif t == "K":
                kanji[f"『{p[1]}』@{p[2]}号"] += 1

    def lst(c):
        return [[k, v] for k, v in c.most_common()]

    return {
        "racer": lst(racer),
        "racer_lane": lst(racer_lane),
        "venue": lst(venue),
        "lane": lst(lane),
        "motor_last": lst(motor_last),
        "toban_last": lst(toban_last),
        "kanji": lst(kanji),
        "types": lst(types),
    }


def build_all(conn, *, min_support_cond=15, min_support_role=8):
    """In-memory バンドル(タプルを保持)。"""
    races = load_races(conn)
    base = base_rates(races)
    conditions = mine_are_conditions(races, min_support=min_support_cond, target_tier=2)
    top_conditions = conditions[:300]
    return {
        "race_count": len(races),
        "base": base,
        "conditions": top_conditions,
        "arashi_lane3": arashi_lane3(races, min_support=min_support_role),
        "regulars": are_regulars(races, min_support=min_support_cond),
        "venues": rough_venues(races),
        "common": common_factors(top_conditions),
        "lane_top3": mine_lane_top3(races, min_support=min_support_role + 4),
    }


def _card_to_info(lanes, venue, rno, date):
    """racecard(結果なし)を preds_of が読める info 形へ。"""
    lmap = {}
    for e in lanes:
        m = e.get("motor")
        lmap[int(e["lane"])] = {
            "name": _clean_name(e.get("name")),
            "toban": e.get("toban"),
            "motor": int(m) if m is not None else None,
        }
    return {"date": date, "rno": int(rno), "venue": venue, "lanes": lmap}


def evaluate_card(lanes, venue, rno, date, bundle, *, fire_lift_min=1.8):
    """出走表(結果前)を bundle の条件で評価。

    are_score = 発火条件の最大 lift(無ければ 1.0)。
    level: are_score>=2.5 -> 万舟警報 ; >=fire_lift_min -> やや荒れ ; else 堅め。
    """
    info = _card_to_info(lanes, venue, rno, date)
    pset = set(preds_of(info))
    fired = []
    for d in bundle["conditions"]:
        if all(p in pset for p in d["cond"]):
            fired.append(d)
    if fired:
        are_score = max(d["lift"] for d in fired)
    else:
        are_score = 1.0
    if are_score >= 2.5:
        level = "万舟警報"
    elif are_score >= fire_lift_min:
        level = "やや荒れ"
    else:
        level = "堅め"
    fired_sorted = sorted(fired, key=lambda x: -x["lift"])[:8]
    return {
        "are_score": are_score,
        "level": level,
        "n_fired": len(fired),
        "fired": [{
            "desc": d["desc"],
            "conf": d["conf"],
            "lift": d["lift"],
            "expect_index": d["expect_index"],
            "soft_rate": d["soft_rate"],
            "support": d["support"],
        } for d in fired_sorted],
    }


def verify_period(conn, from_date, to_date, bundle, *, fire_lift_min=1.8):
    """[from_date,to_date] の女子戦・結果ありレースを bundle["conditions"] で検証。"""
    races = load_races(conn)
    period = {k: v for k, v in races.items()
              if from_date <= v["date"] <= to_date}
    n = len(period)
    base_hit = sum(1 for i in period.values() if i["tier"] >= 2)
    base_rate = base_hit / n if n else 0.0

    # lift>=fire_lift_min の条件のみ採用
    active = [d for d in bundle["conditions"] if d["lift"] >= fire_lift_min]

    fired_races = 0
    manshu_in_fired = 0
    soft_in_fired = 0
    paysum_man = 0
    paycnt_man = 0
    by_level = {
        "万舟警報": {"fired": 0, "manshu": 0},
        "やや荒れ": {"fired": 0, "manshu": 0},
    }
    for info in period.values():
        pset = set(preds_of(info))
        best = 0.0
        any_fire = False
        for d in active:
            if all(p in pset for p in d["cond"]):
                any_fire = True
                if d["lift"] > best:
                    best = d["lift"]
        if not any_fire:
            continue
        fired_races += 1
        is_man = info["tier"] >= 2
        if info["tier"] >= 1:
            soft_in_fired += 1
        if is_man:
            manshu_in_fired += 1
            if info["payout"] is not None:
                paysum_man += info["payout"]
                paycnt_man += 1
        lvl = "万舟警報" if best >= 2.5 else "やや荒れ"
        by_level[lvl]["fired"] += 1
        if is_man:
            by_level[lvl]["manshu"] += 1

    hit_rate = manshu_in_fired / fired_races if fired_races else 0.0
    avg_pay_man = paysum_man / paycnt_man if paycnt_man else 0.0
    # payout_3t_yen は「100円賭けの払戻」なので、1レース100円ずつ張る前提だと
    # 期待払戻(円/100円賭け) = hit_rate × 平均万舟配当。
    # 回収率(%) = 払戻総額 ÷ 賭け金総額 × 100 = (その期待払戻) ÷ 100 × 100 = 期待払戻そのもの。
    approx_return = hit_rate * avg_pay_man
    out_by_level = {}
    for lvl, d in by_level.items():
        out_by_level[lvl] = {
            "fired": d["fired"],
            "manshu": d["manshu"],
            "rate": (d["manshu"] / d["fired"]) if d["fired"] else 0.0,
        }
    return {
        "from": from_date,
        "to": to_date,
        "races": n,
        "fired_races": fired_races,
        "manshu_in_fired": manshu_in_fired,
        "hit_rate": hit_rate,
        "base_rate": base_rate,
        "lift": (hit_rate / base_rate) if base_rate > 0 else 0.0,
        "soft_hit_rate": (soft_in_fired / fired_races) if fired_races else 0.0,
        "avg_pay_manshu": avg_pay_man,
        "approx_return_index": approx_return,
        "approx_roi_pct": approx_return,  # =回収率%(100%で収支トントン)。買い目1点・的中=平均配当の楽観上限
        "by_level": out_by_level,
    }


# ----------------------------------------------------------------------------
# オカルト → 具体的な買い目(3連単)
# ----------------------------------------------------------------------------
def score_boats(lanes, venue, rno, date, bundle):
    """カードの各艇を「3着内に来そうか」のオカルトスコアで評価。

    score = Σ max(0, lift-1) × log1p(support) over その艇に効く lane依存述語
            (選手@号 / 登番末尾@号 / M末尾@号 / 漢字@号)。
    勝率・モーター2連率・級別など一般情報は一切使わない(bundle["lane_top3"]
    のオカルト lift だけ)。
    返り値: スコア降順の boats[{lane,name,score,signals[]}]。
    """
    import math
    info = _card_to_info(lanes, venue, rno, date)
    lt = bundle.get("lane_top3") or {"base": {}, "signals": {}}
    sig = lt.get("signals") or {}
    per = {}
    for e in lanes:
        L = int(e["lane"])
        per[L] = {"lane": L, "name": _clean_name(e.get("name")),
                  "score": 0.0, "signals": []}
    for p in preds_of(info):
        d = sig.get(p)
        if not d:
            continue
        L = d["lane"]
        if L not in per:
            continue
        lift = d["lift"]
        contrib = (lift - 1.0) * math.log1p(d["support"]) if lift > 1.0 else 0.0
        per[L]["score"] += contrib
        per[L]["signals"].append({
            "desc": d["desc"], "lift": lift,
            "support": d["support"], "conf": d["conf"],
        })
    boats = sorted(per.values(), key=lambda x: (-x["score"], x["lane"]))
    for b in boats:
        b["signals"] = sorted(b["signals"], key=lambda s: -s["lift"])[:5]
    return boats


def _formation(ranked, level):
    """ranked: スコア降順 lane(int) リスト。level に応じ 1/2/3着の候補幅を決め
    3連単 combos(set of (l1,l2,l3)) と structure を返す。荒れるほど穴まで広げる。"""
    n = len(ranked)

    def take(k):
        return ranked[:min(k, n)]

    if level == "万舟警報":
        first, second, third = take(2), take(5), take(6)
    elif level == "やや荒れ":
        first, second, third = take(2), take(4), take(5)
    else:  # 堅め
        first, second, third = take(1), take(3), take(5)

    combos = set()
    for a in first:
        for b in second:
            if b == a:
                continue
            for c in third:
                if c == a or c == b:
                    continue
                combos.add((a, b, c))
    return combos, {"first": first, "second": second, "third": third}


def suggest_tickets(lanes, venue, rno, date, bundle, level=None):
    """オカルトスコア順 → 荒れ度に応じた 3連単フォーメーションを生成。"""
    boats = score_boats(lanes, venue, rno, date, bundle)
    if level is None:
        level = evaluate_card(lanes, venue, rno, date, bundle)["level"]
    ranked = [b["lane"] for b in boats]
    combos, structure = _formation(ranked, level)
    combos_sorted = sorted(combos)
    points = len(combos_sorted)
    return {
        "level": level,
        "boats": boats,
        "ranked": ranked,
        "structure": structure,
        "combos": [list(c) for c in combos_sorted],
        "points": points,
        "cost_yen": points * 100,
    }


def backtest_tickets(conn, from_date, to_date, bundle):
    """[from_date,to_date] の女子戦・結果ありレースで、オカルト買い目を実際に
    買い続けたら回収率がどうなるかを検証(¥100/点)。

    回収率% = 払戻総額 ÷ 賭け金総額 × 100。3連単は的中したレースで
    payout_3t_yen(=100円賭けの払戻)が丸ごと返る前提。
    ※ bundle は同じ全期間から採掘しているためインサンプル(楽観寄り)。
    """
    races = load_races(conn)
    period = [v for v in races.values() if from_date <= v["date"] <= to_date]

    total_cost = 0
    total_return = 0
    bet_races = 0
    hit_races = 0
    pts_sum = 0
    lv0 = {"races": 0, "hits": 0, "cost": 0, "ret": 0, "points": 0}
    by_level = {"万舟警報": dict(lv0), "やや荒れ": dict(lv0), "堅め": dict(lv0)}

    for info in period:
        order = info.get("finish_order") or []
        if len(order) < 3:
            continue
        lanes = [{"lane": L, "toban": ed.get("toban"),
                  "name": ed.get("name"), "motor": ed.get("motor")}
                 for L, ed in info["lanes"].items()]
        ev = evaluate_card(lanes, info["venue"], info["rno"], info["date"], bundle)
        sug = suggest_tickets(lanes, info["venue"], info["rno"], info["date"],
                              bundle, level=ev["level"])
        pts = sug["points"]
        if pts == 0:
            continue
        cost = pts * 100
        lvl = sug["level"]
        bet_races += 1
        pts_sum += pts
        total_cost += cost
        by_level[lvl]["races"] += 1
        by_level[lvl]["cost"] += cost
        by_level[lvl]["points"] += pts
        actual = (order[0], order[1], order[2])
        if actual in {tuple(c) for c in sug["combos"]}:
            pay = info["payout"] or 0
            hit_races += 1
            total_return += pay
            by_level[lvl]["hits"] += 1
            by_level[lvl]["ret"] += pay

    roi = (total_return / total_cost * 100) if total_cost else 0.0
    out_levels = {}
    for lvl, d in by_level.items():
        out_levels[lvl] = {
            "races": d["races"],
            "hits": d["hits"],
            "hit_rate": (d["hits"] / d["races"]) if d["races"] else 0.0,
            "cost_yen": d["cost"],
            "return_yen": d["ret"],
            "roi_pct": (d["ret"] / d["cost"] * 100) if d["cost"] else 0.0,
            "avg_points": (d["points"] / d["races"]) if d["races"] else 0.0,
        }
    return {
        "from": from_date,
        "to": to_date,
        "bet_races": bet_races,
        "hit_races": hit_races,
        "hit_rate": (hit_races / bet_races) if bet_races else 0.0,
        "avg_points": (pts_sum / bet_races) if bet_races else 0.0,
        "cost_yen": total_cost,
        "return_yen": total_return,
        "roi_pct": roi,
        "by_level": out_levels,
        "in_sample": True,
    }


if __name__ == "__main__":
    BASE = os.path.dirname(os.path.abspath(__file__))
    DBP = os.path.join(BASE, "kyotei_sign.db")
    conn = sqlite3.connect(f"file:{DBP}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    bundle = build_all(conn)
    b = bundle["base"]
    print(f"=== 荒れエンジン 自己テスト ===")
    print(f"対象レース: {bundle['race_count']} (女子戦・結果あり)")
    print(f"ベース率: 万舟 {b['manshu_rate']*100:.1f}%  "
          f"やや荒れ {b['soft_rate']*100:.1f}%  n={b['n']}  "
          f"平均万舟配当 ¥{int(b['avg_pay_manshu']):,}")

    print(f"\n=== 荒れ常連 TOP15 (出走するだけで万舟率↑, lift順) ===")
    print(f"{'万舟/出走':>10} {'万舟率':>6} {'lift':>5} {'soft率':>6} {'平均万舟':>9}  選手")
    for d in bundle["regulars"][:15]:
        print(f"{d['hits']:>4}/{d['support']:<5}{d['conf']*100:5.0f}% "
              f"{d['lift']:5.1f} {d['soft_rate']*100:5.0f}% "
              f"¥{int(d['avg_pay_manshu']):>8,}  {d['name']}")

    print(f"\n=== 3号艇 荒らし役 TOP15 (3号で座ると荒れる, lift順) ===")
    print(f"{'万舟/3号':>10} {'万舟率':>6} {'lift':>5} {'平均万舟':>9}  選手")
    for d in bundle["arashi_lane3"][:15]:
        print(f"{d['hits']:>4}/{d['support']:<5}{d['conf']*100:5.0f}% "
              f"{d['lift']:5.1f} ¥{int(d['avg_pay_manshu']):>8,}  {d['name']}")

    print(f"\n=== 会場ランキング (荒れ水面, 万舟率順 / 全{len(bundle['venues'])}場) ===")
    print(f"{'万舟/開催':>11} {'万舟率':>6} {'soft率':>6} {'lift':>5} {'平均万舟':>9}  会場")
    for d in bundle["venues"]:
        mark = "  ★" if d["venue"] in ("浜名湖", "平和島") else ""
        print(f"{d['hits']:>5}/{d['support']:<5}{d['conf']*100:5.1f}% "
              f"{d['soft_rate']*100:5.0f}% {d['lift']:5.2f} "
              f"¥{int(d['avg_pay_manshu']):>8,}  {d['venue']}{mark}")

    print(f"\n=== 荒れ条件 TOP20 (lift順, support>=15) ===")
    print(f"{'万舟/発火':>10} {'万舟率':>6} {'lift':>5} {'期待指数':>8}  条件")
    for d in bundle["conditions"][:20]:
        print(f"{d['hits']:>4}/{d['support']:<5}{d['conf']*100:5.0f}% "
              f"{d['lift']:5.1f} {int(d['expect_index']):>8,}  {d['desc']}")

    print(f"\n=== 共通要素サマリー (lift上位{min(80,len(bundle['conditions']))}条件) ===")
    cm = bundle["common"]

    def show_cm(label, items, n=12, minc=2):
        f = [x for x in items if x[1] >= minc][:n]
        body = ", ".join(f"{k}×{v}" for k, v in f) if f else "(繰り返しなし)"
        print(f"  {label}: {body}")

    show_cm("選手", cm["racer"])
    show_cm("選手×号艇", cm["racer_lane"])
    show_cm("会場", cm["venue"])
    show_cm("号艇", cm["lane"], minc=1)
    show_cm("M末尾", cm["motor_last"])
    show_cm("登番末尾", cm["toban_last"])
    show_cm("漢字", cm["kanji"])
    print(f"  述語タイプ分布: {dict(cm['types'])}")

    lt = bundle["lane_top3"]
    print(f"\n=== 号艇別 3着内 基礎率(内部正規化用) ===")
    print("  " + "  ".join(f"{L}号 {lt['base'].get(L,0)*100:.0f}%"
                           for L in sorted(lt['base'])))
    print(f"  lane依存シグナル数(support>=12): {len(lt['signals'])}")
    top_sig = sorted(lt["signals"].values(), key=lambda x: -x["lift"])[:12]
    print(f"\n=== 来る艇シグナル TOP12 (3着内 lift順) ===")
    print(f"{'的中/発火':>10} {'率':>5} {'lift':>5} {'号':>3}  述語")
    for d in top_sig:
        print(f"{d['hits']:>4}/{d['support']:<5}{d['conf']*100:4.0f}% "
              f"{d['lift']:5.2f} {d['lane']:>2}号  {d['desc']}")

    # サンプル買い目(最初の女子戦カードで実演)
    races = load_races(conn)
    sample = next(iter(races.values()))
    s_lanes = [{"lane": L, "toban": ed.get("toban"),
                "name": ed.get("name"), "motor": ed.get("motor")}
               for L, ed in sample["lanes"].items()]
    sug = suggest_tickets(s_lanes, sample["venue"], sample["rno"], sample["date"], bundle)
    print(f"\n=== サンプル買い目 {sample['date']} {sample['venue']}{sample['rno']}R "
          f"[{sug['level']}] ===")
    for b in sug["boats"]:
        tag = ", ".join(s["desc"] for s in b["signals"][:2]) or "-"
        print(f"  {b['lane']}号 {b['name']:<8} score={b['score']:5.2f}  {tag}")
    st = sug["structure"]
    print(f"  1着{st['first']} → 2着{st['second']} → 3着{st['third']}")
    print(f"  {sug['points']}点 / ¥{sug['cost_yen']:,}  実結果={sample.get('finish_order')}")

    print(f"\n=== 買い目バックテスト(全期間・インサンプル) ===")
    dates = sorted(v["date"] for v in races.values())
    bt = backtest_tickets(conn, dates[0], dates[-1], bundle)
    print(f"  対象 {bt['bet_races']}レース  的中 {bt['hit_races']}  "
          f"的中率 {bt['hit_rate']*100:.1f}%  平均 {bt['avg_points']:.1f}点")
    print(f"  賭金 ¥{bt['cost_yen']:,}  払戻 ¥{bt['return_yen']:,}  "
          f"回収率 {bt['roi_pct']:.1f}%")
    for lvl, d in bt["by_level"].items():
        if d["races"]:
            print(f"    {lvl:>6}: {d['races']:>4}R 的中{d['hits']:>3} "
                  f"({d['hit_rate']*100:4.1f}%) 回収率 {d['roi_pct']:6.1f}% "
                  f"平均{d['avg_points']:.0f}点")

    conn.close()
