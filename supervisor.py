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
    import sqlite3
    # DBロック（collect.pyの書き込み）に備えてリトライ
    for _ in range(20):
        try:
            with db.get_conn() as conn:
                conn.execute('UPDATE races SET is_ladies=1 WHERE is_ladies=0')
                conn.commit()
                races = miner.load_races(conn, ladies_only=True)
                signs = miner.mine_signs(races)
                miner.save_signs(conn, signs)
            return signs
        except sqlite3.OperationalError:
            time.sleep(5)
    return []

def write_status(d):
    json.dump(d, open(STATUS,'w'), ensure_ascii=False, indent=2, default=str)

def collect_running():
    # collect.py backfill / collect_range / launcher のいずれでも検出
    for pat in ('collect.py backfill', 'collect_range', 'collect.collect_range'):
        if subprocess.run(['pgrep','-f',pat], capture_output=True).returncode == 0:
            return True
    return False

while True:
    try:
        db.init_db()
        for _ in range(20):
            import sqlite3
            try:
                with db.get_conn() as conn:
                    stats = db.db_stats(conn)
                break
            except sqlite3.OperationalError:
                time.sleep(5)
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
                 'conf':s['confidence'],'lift':s['lift']} for s in signs[:15]
            ],
        }
        write_status(st)
        if not collect_running():
            st['stopped'] = 'backfill_done'; write_status(st); break
    except Exception as e:
        # 何が起きても supervisor は死なせない
        import traceback; traceback.print_exc()
    time.sleep(900)
