#!/usr/bin/env python3
"""デタッチ監視: 15分ごとに export→dataブランチpush→マイニング。サイン3つで停止フラグ。"""
import json, os, subprocess, time, datetime
os.chdir('/Users/ohyabumasaya/Desktop/kyotei_sign')
import db, miner

DATA_DIR = '/tmp/kyotei-data'
STATUS = '/tmp/kyotei_status.json'

def export_and_push():
    with db.get_conn() as conn:
        data = {
            'exported_at': datetime.datetime.now().isoformat(), 'schema_version': 1, 'source': 'local',
            'races':[dict(r) for r in conn.execute('SELECT * FROM races')],
            'race_entries':[dict(r) for r in conn.execute('SELECT * FROM race_entries')],
            'race_results':[dict(r) for r in conn.execute('SELECT * FROM race_results')],
            'signs':[dict(r) for r in conn.execute('SELECT * FROM signs')],
        }
    content = json.dumps(data, ensure_ascii=False, separators=(',',':'))
    def _sync_write_commit_push(msg):
        subprocess.run(['git','fetch','-q','origin','data'], cwd=DATA_DIR, capture_output=True)
        subprocess.run(['git','reset','--hard','origin/data'], cwd=DATA_DIR, capture_output=True)
        open(os.path.join(DATA_DIR,'data.json'),'w',encoding='utf-8').write(content)
        subprocess.run(['git','add','data.json'], cwd=DATA_DIR)
        if subprocess.run(['git','diff','--cached','--quiet'], cwd=DATA_DIR).returncode == 0:
            return True  # 差分なし=既に最新
        subprocess.run(['git','commit','-q','-m',msg], cwd=DATA_DIR)
        return subprocess.run(['git','push','-q','origin','data'], cwd=DATA_DIR, capture_output=True).returncode == 0
    ok = _sync_write_commit_push(f'Local {datetime.datetime.utcnow():%Y-%m-%dT%H:%MZ} races={len(data["races"])}')
    if not ok:  # 競合したら1回だけ再試行
        _sync_write_commit_push(f'Local retry races={len(data["races"])}')
    return len(data['races'])

def mine():
    with db.get_conn() as conn:
        # collect は女子戦会場しか取らないので、全レースを女子戦に確定
        # （ＶＳ等の略称でキーワード判定を漏れた分を救済）
        conn.execute('UPDATE races SET is_ladies=1 WHERE is_ladies=0')
        conn.commit()
        races = miner.load_races(conn, ladies_only=True)
        signs = miner.mine_signs(races)
        miner.save_signs(conn, signs)
    return signs

def write_status(d):
    json.dump(d, open(STATUS,'w'), ensure_ascii=False, indent=2, default=str)

while True:
    db.init_db()
    with db.get_conn() as conn:
        stats = db.db_stats(conn)
    n_races = export_and_push()
    signs = mine()
    st = {
        'updated': datetime.datetime.now().isoformat(),
        'races': stats['races'], 'ladies_races': stats['ladies_races'],
        'results': stats['race_results'], 'date_range': stats['date_range'],
        'signs_count': len(signs),
        'top_signs': [
            {'racer':s['racer_name'],'lane':s['cond_lane'],'kind':s['target_kind'],
             'target':s['target_pair'],'hits':s['hits'],'support':s['support'],
             'conf':s['confidence'],'lift':s['lift']} for s in signs[:10]
        ],
    }
    write_status(st)
    # collect 終了 or サイン3つで停止
    collect_alive = subprocess.run(['pgrep','-f','collect.py backfill'], capture_output=True).returncode == 0
    if len(signs) >= 3:
        st['stopped'] = 'signs>=3'; write_status(st); break
    if not collect_alive:
        st['stopped'] = 'backfill_done'; write_status(st); break
    time.sleep(900)
