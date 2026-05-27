"""
バックテスト & データ整合性検証

【バックテスト】
学習期間で抽出したサインを、テスト期間（学習期間より後）の実レースに当てはめ、
発火→的中の回数・回収率を測る。

【データ整合性】
- race_entries が 6 艇揃っているか
- race_results.finish_json が 6 艇分あるか
- 結果に登場する艇番が出走表とマッチするか
- 配当値が None でないか
"""
from __future__ import annotations

import json
import datetime
from collections import defaultdict
from typing import Optional

import db
import miner


# ── データ整合性チェック ─────────────────────────────────────────
def check_integrity(conn, *, ladies_only: bool = True) -> dict:
    issues = {
        "missing_entries": [],
        "missing_finish": [],
        "lane_mismatch": [],
        "missing_payout": [],
        "summary": {},
    }
    sql = """
        SELECT r.date, r.jcd, r.rno, r.is_ladies, r.has_card, r.has_result,
               (SELECT COUNT(*) FROM race_entries e WHERE e.date=r.date AND e.jcd=r.jcd AND e.rno=r.rno) AS entry_cnt,
               rr.finish_json, rr.payout_3t_yen
        FROM races r
        LEFT JOIN race_results rr ON r.date=rr.date AND r.jcd=rr.jcd AND r.rno=rr.rno
    """
    params: list = []
    if ladies_only:
        sql += " WHERE r.is_ladies=1"
    total = 0
    ok = 0
    for row in conn.execute(sql, params):
        total += 1
        key = f"{row['date']} {row['jcd']} {row['rno']}R"
        race_ok = True

        if row["has_card"] and row["entry_cnt"] != 6:
            issues["missing_entries"].append(
                {"race": key, "have": row["entry_cnt"], "want": 6}
            )
            race_ok = False
        if row["has_result"]:
            if not row["finish_json"]:
                issues["missing_finish"].append({"race": key})
                race_ok = False
            else:
                fin = json.loads(row["finish_json"])
                if len(fin) < 6:
                    issues["missing_finish"].append(
                        {"race": key, "have": len(fin), "want": 6}
                    )
                    race_ok = False
                # 結果に出てくる艇番が出走表とマッチするか
                if row["has_card"]:
                    entry_lanes = set(
                        e["lane"] for e in conn.execute(
                            "SELECT lane FROM race_entries WHERE date=? AND jcd=? AND rno=?",
                            (row["date"], row["jcd"], row["rno"]),
                        )
                    )
                    finish_lanes = set(f[1] for f in fin)
                    if entry_lanes != finish_lanes:
                        issues["lane_mismatch"].append({
                            "race": key,
                            "entry_lanes": sorted(entry_lanes),
                            "finish_lanes": sorted(finish_lanes),
                        })
                        race_ok = False
            if row["payout_3t_yen"] is None:
                issues["missing_payout"].append({"race": key})
                race_ok = False
        if race_ok:
            ok += 1

    issues["summary"] = {
        "total_races": total,
        "ok": ok,
        "issues": total - ok,
        "missing_entries": len(issues["missing_entries"]),
        "missing_finish": len(issues["missing_finish"]),
        "lane_mismatch": len(issues["lane_mismatch"]),
        "missing_payout": len(issues["missing_payout"]),
    }
    return issues


# ── バックテスト ─────────────────────────────────────────────────
def run_backtest(conn, *,
                 train_from: str, train_to: str,
                 test_from: str, test_to: str,
                 min_support: int = 10,
                 min_confidence: float = 0.80,
                 min_lift: float = 1.5,
                 max_p_value: float = 0.05) -> dict:
    """
    train期間でサインを抽出 → test期間で発火→実結果を照合し、
    的中率と購入シミュレーション（3連複1点 or 1着単点）の回収率を返す。
    """
    train_races = miner.load_races(conn, ladies_only=True,
                                   date_from=train_from, date_to=train_to)
    if not train_races:
        return {"error": "学習期間にデータがない", "train_races": 0}

    signs = miner.mine_signs(train_races,
                             min_support=min_support,
                             min_confidence=min_confidence,
                             min_lift=min_lift,
                             max_p_value=max_p_value)

    # サインを (toban, lane) でインデックス化
    sign_index: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for s in signs:
        sign_index[(s["toban"], s["cond_lane"])].append(s)

    test_races = miner.load_races(conn, ladies_only=True,
                                  date_from=test_from, date_to=test_to)

    per_kind = {
        "top3_pair": {"fires": 0, "hits": 0, "stake": 0, "payout": 0, "samples": []},
        "win": {"fires": 0, "hits": 0, "stake": 0, "payout": 0, "samples": []},
    }
    overall_fires = 0
    overall_hit_races = 0

    for race in test_races:
        top3 = {f[1] for f in race["finish"][:3]}
        top1 = race["finish"][0][1] if race["finish"] else None

        # 配当取得
        row = conn.execute(
            "SELECT payout_3t, payout_3t_yen, payout_3f, payout_3f_yen FROM race_results WHERE date=? AND jcd=? AND rno=?",
            (race["date"], race["jcd"], race["rno"]),
        ).fetchone()
        if not row:
            continue
        pay_3t_yen = row["payout_3t_yen"] or 0
        pay_3f_yen = row["payout_3f_yen"] or 0
        pay_3t_combo = row["payout_3t"] or ""
        pay_3f_combo = row["payout_3f"] or ""

        fired_this_race = False
        for lane, toban, _ in race["entries"]:
            if not toban:
                continue
            for sign in sign_index.get((toban, lane), []):
                kind = sign["target_kind"]
                target = sign["target_pair"]
                fired_this_race = True
                per_kind[kind]["fires"] += 1

                if kind == "top3_pair":
                    a, b = (int(x) for x in target.split("-"))
                    hit = (a in top3 and b in top3)
                    # 3連複ペアに購入する想定: 残り1艇は1〜6の任意 → 4点買い
                    # でも実際の3連複配当はその組番のみ。シンプルに「サイン的中=ヒット」として
                    # 「3連複(a=b=c)」全パターンの的中があれば payout を加算するモデル
                    if hit:
                        per_kind[kind]["hits"] += 1
                        # a, b を含む3連複が支払額になる
                        third = next(iter(top3 - {a, b}))
                        per_kind[kind]["payout"] += pay_3f_yen
                    # ステーク: 1艇あたり100円 × 残り4艇 = 400円
                    per_kind[kind]["stake"] += 400
                else:  # win
                    m = int(target)
                    hit = (top1 == m)
                    if hit:
                        per_kind[kind]["hits"] += 1
                        # 単勝想定で配当のうち取り分
                        # ※単勝配当は payouts に含まれないので便宜的に 3連単/6 を仮置き
                        per_kind[kind]["payout"] += pay_3t_yen // 6 if pay_3t_yen else 0
                    per_kind[kind]["stake"] += 100

                if len(per_kind[kind]["samples"]) < 30:
                    per_kind[kind]["samples"].append({
                        "race": f"{race['date']} {race['jcd']} {race['rno']}R",
                        "sign": f"{sign['racer_name']}@{lane}号艇→{kind}={target}",
                        "hit": hit,
                    })

        if fired_this_race:
            overall_fires += 1
            # サインのどれかが当たれば「ヒットレース」とカウント
            for lane, toban, _ in race["entries"]:
                if not toban:
                    continue
                for sign in sign_index.get((toban, lane), []):
                    if sign["target_kind"] == "top3_pair":
                        a, b = (int(x) for x in sign["target_pair"].split("-"))
                        if a in top3 and b in top3:
                            overall_hit_races += 1
                            break
                    elif sign["target_kind"] == "win":
                        if top1 == int(sign["target_pair"]):
                            overall_hit_races += 1
                            break
                else:
                    continue
                break

    for k in per_kind:
        d = per_kind[k]
        d["accuracy"] = d["hits"] / d["fires"] if d["fires"] else 0.0
        d["roi"] = d["payout"] / d["stake"] if d["stake"] else 0.0

    return {
        "train_period": [train_from, train_to],
        "test_period": [test_from, test_to],
        "train_races": len(train_races),
        "test_races": len(test_races),
        "signs_extracted": len(signs),
        "overall_fires": overall_fires,
        "overall_hit_races": overall_hit_races,
        "overall_race_hit_rate": (overall_hit_races / overall_fires) if overall_fires else 0.0,
        "per_kind": per_kind,
        "thresholds": {
            "min_support": min_support,
            "min_confidence": min_confidence,
            "min_lift": min_lift,
            "max_p_value": max_p_value,
        },
    }


if __name__ == "__main__":
    import sys
    db.init_db()
    with db.get_conn() as conn:
        if "--integrity" in sys.argv:
            r = check_integrity(conn)
            import json as _json
            print(_json.dumps(r["summary"], ensure_ascii=False, indent=2))
        elif "--backtest" in sys.argv:
            # デフォルト: 学習=全期間-3ヶ月、テスト=直近3ヶ月
            today = datetime.date.today()
            test_to = today.strftime("%Y%m%d")
            test_from = (today - datetime.timedelta(days=90)).strftime("%Y%m%d")
            train_to = (today - datetime.timedelta(days=91)).strftime("%Y%m%d")
            train_from = "20000101"
            r = run_backtest(conn, train_from=train_from, train_to=train_to,
                             test_from=test_from, test_to=test_to)
            import json as _json
            print(_json.dumps({k: v for k, v in r.items() if k != "per_kind"},
                              ensure_ascii=False, indent=2))
            print("--- per_kind ---")
            for k, v in r.get("per_kind", {}).items():
                print(f"[{k}] fires={v['fires']} hits={v['hits']} "
                      f"acc={v['accuracy']:.1%} ROI={v['roi']:.2f}")
        else:
            print("Usage: python3 backtest.py --integrity | --backtest")
