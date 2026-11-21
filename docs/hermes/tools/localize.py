"""Repoint every cloud-model session at the LOCAL engine. codex-worker keeps openai-codex."""
import sys, json, glob, pathlib, collections
sys.path.insert(0, '/home/clara/.hermes/hermes-agent')
from hermes_state import SessionDB

LIVE='qwen3.8-flash-next-iq4_xs'; URL='http://127.0.0.1:8081/v1'
CLOUDY=('qwen3.8-27b','gemma4','kimi','muse','glm-5','minimax','gpt-5','gpt-6','seed-2','gemma','qwen3.5','qwen3-vl',
        'nemotron','deepseek-v4.1','mistral-large','gpt-oss','qwen3.8-flash-next-iq3','qwen3.8-flash-next-q4_k_xl',
        'qwen3.8-flash-next-q3_xl','qwen3.8-35b','glm-ocr','mistral-small')
KEEP_PROVIDERS={'openai-codex'}   # the local codex runtime

def is_cloud_model(m):
    return bool(m) and any(h in m for h in CLOUDY)

total=0; kept=0
for db_path in ['/home/clara/.hermes/state.db']+sorted(glob.glob('/home/clara/.hermes/profiles/*/state.db')):
    prof='ROOT' if db_path.endswith('/.hermes/state.db') else db_path.split('/profiles/')[1].split('/')[0]
    db=SessionDB(pathlib.Path(db_path))
    with db._read_ctx() as conn:
        rows=conn.execute("select id, model, model_config, source from sessions").fetchall()
    n=0; k=0
    for sid, model, mc, src in rows:
        if not is_cloud_model(model): continue
        try: cfg=json.loads(mc) if mc else {}
        except Exception: cfg={}
        prov=str(cfg.get('provider') or '')
        if prov in KEEP_PROVIDERS:
            k+=1; continue
        db.update_session_model(sid, LIVE, provider='strata', base_url=URL, api_mode='chat_completions')
        n+=1
    if n or k:
        print("  %-20s repointed %-4d  kept(codex) %d" % (prof, n, k))
    total+=n; kept+=k
    db.close()
print("  TOTAL repointed to local: %d   (kept on the codex runtime: %d)" % (total, kept))
