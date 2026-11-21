import sys, json, glob, pathlib
sys.path.insert(0, '/home/clara/.hermes/hermes-agent')
from hermes_state import SessionDB
LIVE='qwen3.8-flash-next-iq4_xs'; URL='http://127.0.0.1:8081/v1'
n=0
for db_path in ['/home/clara/.hermes/state.db']+sorted(glob.glob('/home/clara/.hermes/profiles/*/state.db')):
    db=SessionDB(pathlib.Path(db_path))
    with db._read_ctx() as conn:
        rows=conn.execute("select id, model, model_config from sessions where ended_at is null and archived=0").fetchall()
    for sid, model, mc in rows:
        try: cfg=json.loads(mc) if mc else {}
        except Exception: cfg={}
        prov=str(cfg.get('provider') or ''); bu=str(cfg.get('base_url') or '')
        if 'cloud' in prov or 'ollama.com' in bu or 'ark.ap-southeast' in bu:
            db.update_session_model(sid, LIVE, provider='strata', base_url=URL, api_mode='chat_completions')
            print("  pinned local: %-34s %s -> %s" % (sid[:34], (model or '')[:26], LIVE)); n+=1
    db.close()
print("  pinned: %d" % n)
