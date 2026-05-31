#!/usr/bin/env python3
"""is_ladies 誤判定(併催の男子戦混入)を出走選手の女子比率で再判定して修正する
ワンタイム・クリーンアップ。

背景:
  collect.py は以前「女子戦会場のレースを会場一括で is_ladies=1」にしていたため、
  併催の男子戦(チャレンジカップ / マスターズVSルーキーズ / レディースVSルーキーズ
  バトルの男子側など)が女子戦として紛れ込んでいた。
  → 出走選手の女子比率(>=50%)でレース単位に再判定して修正する。

使い方:
  python3 fix_ladies.py            # DRY-RUN (変更内容を表示するだけ)
  python3 fix_ladies.py --apply    # 実際に UPDATE して commit
"""
import sys
from collections import Counter

import db


def main() -> None:
    apply = "--apply" in sys.argv
    db.init_db()
    with db.get_conn() as conn:
        female = db.build_female_toban(conn)
        print(f"女子roster(純女子戦タイトルの出走toban): {len(female)} 人")
        if len(female) < 100:
            print("⚠ rosterが薄すぎる(<100)。母集団不足のため中止。")
            return

        rows = conn.execute(
            "SELECT date, jcd, rno, title, is_ladies FROM races"
        ).fetchall()
        to0, to1 = [], []
        for r in rows:
            want = db.race_is_ladies(
                conn, r["date"], r["jcd"], r["rno"], female, r["title"]
            )
            if r["is_ladies"] == 1 and want == 0:
                to0.append(r)
            elif r["is_ladies"] == 0 and want == 1:
                to1.append(r)

        print(f"\n全レース: {len(rows)}")
        print(f"  is_ladies 1→0 (女子戦から除外): {len(to0)} レース")
        print(f"  is_ladies 0→1 (女子戦へ追加)  : {len(to1)} レース")

        def breakdown(label, lst):
            print(f"\n【{label}】タイトル別内訳(上位20)")
            c = Counter((r["title"] or "(無題)") for r in lst)
            for title, n in c.most_common(20):
                print(f"  {n:>4}  {title}")

        if to0:
            breakdown("1→0 除外されるレース", to0)
        if to1:
            breakdown("0→1 追加されるレース", to1)

        if apply:
            for r in to0:
                conn.execute(
                    "UPDATE races SET is_ladies=0 WHERE date=? AND jcd=? AND rno=?",
                    (r["date"], r["jcd"], r["rno"]),
                )
            for r in to1:
                conn.execute(
                    "UPDATE races SET is_ladies=1 WHERE date=? AND jcd=? AND rno=?",
                    (r["date"], r["jcd"], r["rno"]),
                )
            conn.commit()
            print(f"\n✅ APPLIED: {len(to0)+len(to1)} レースを更新してcommitした。")
        else:
            print("\n(DRY-RUN: 変更は未適用。--apply で実行する)")


if __name__ == "__main__":
    main()
