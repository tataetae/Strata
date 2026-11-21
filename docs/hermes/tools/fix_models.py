"""Repoint stale-model sessions at the live build, via the supported SessionDB API."""
import sys, os, json, glob, pathlib, time
sys.path.insert(0, '/home/clara/.hermes/hermes-agent')
from hermes_state import SessionDB

LIVE = 'qwen3.8-flash-next-iq4_xs'
STALE = ('qwen3.8-flash-next-iq3_s', 'qwen3.8-flash-next-q4_k_xl', 'qwen3.8-flash-next-q3_xl')
STRATA_URL = 'http://127.0.0.1:8081/v1'

dbs = ['/home/clara/.hermes/state.db'] + sorted(glob.glob('/home/clara/.hermes/profiles/*/state.db'))
total = 0
for db_path in dbs:
    prof = 'ROOT' if db_path.endswith('/.hermes/state.db') else db_path.split('/profiles/')[1].split('/')[0]
    db = SessionDB(pathlib.Path(db_path))
    try:
        with db._read_ctx() as conn:
            rows = conn.execute(
                "select id, model, model_config, source, archived from sessions where model in (?,?,?)",
                STALE).fetchall()
    except Exception as e:
        print("  %-22s READ ERROR %s" % (prof, str(e)[:60])); db.close(); continue
    if not rows:
        db.close(); continue
    n = 0
    for sid, model, mc, source, arch in rows:
        try: cfg = json.loads(mc) if mc else {}
        except Exception: cfg = {}
        # keep the profile's provider; only the MODEL NAME was stale.
        provider = cfg.get('provider') or 'strata'
        base_url = cfg.get('base_url') or STRATA_URL
        api_mode = cfg.get('api_mode') or 'chat_completions'
        try:
            db.update_session_model(sid, LIVE, provider=provider, base_url=base_url, api_mode=api_mode)
            n += 1
        except Exception as e:
            print("    %-34s FAILED %s" % (sid[:34], str(e)[:70]))
    total += n
    print("  %-22s repointed %d/%d stale-model sessions -> %s" % (prof, n, len(rows), LIVE))
    db.close()
print("  TOTAL repointed: %d" % total)
