import yaml, glob, os, json, sqlite3, collections
CLOUD_PROV={'ollama-cloud','modelark','openai-codex','openai','anthropic'}
CLOUD_API=('ollama.com','ark.ap-southeast','api.openai.com','anthropic.com')
root=yaml.safe_load(open('/home/clara/.hermes/config.yaml')) or {}
provs=root.get('providers') or {}
def cloud_of(provider, base_url=''):
    p=provs.get(provider) or {}
    api=str(p.get('api') or p.get('endpoint') or base_url or '')
    return (provider in CLOUD_PROV or any(h in api for h in CLOUD_API)), api

def prof_of(f):
    return 'ROOT' if f=='/home/clara/.hermes/config.yaml' else f.split('/profiles/')[1].split('/')[0]

files=['/home/clara/.hermes/config.yaml']+sorted(glob.glob('/home/clara/.hermes/profiles/*/config.yaml'))
print("-- 1) PROFILE DEFAULTS --")
for f in files:
    d=yaml.safe_load(open(f)) or {}
    m=d.get('model') or {}
    prov=str(m.get('provider') or '')
    c,api=cloud_of(prov, str(m.get('base_url') or ''))
    print("  %-20s %-26s %-16s %s" % (prof_of(f), str(m.get('default'))[:26], prov, ('CLOUD '+api[:38]) if c else 'local'))

print()
print("-- 2) AUXILIARY routes (summariser + vision) --")
for f in files:
    d=yaml.safe_load(open(f)) or {}
    for task,cfg in (d.get('auxiliary') or {}).items():
        if not isinstance(cfg,dict): continue
        prov=str(cfg.get('provider') or '')
        c,api=cloud_of(prov, str(cfg.get('base_url') or ''))
        if c: print("  %-20s aux.%-14s %-22s %s CLOUD %s" % (prof_of(f), task, str(cfg.get('model'))[:22], prov, api[:38]))

print()
print("-- 3) SESSION ROWS on a cloud model --")
agg=collections.Counter()
for db in sorted(glob.glob('/home/clara/.hermes/state.db')+glob.glob('/home/clara/.hermes/profiles/*/state.db')):
    prof='ROOT' if db.endswith('/.hermes/state.db') else db.split('/profiles/')[1].split('/')[0]
    c=sqlite3.connect('file:%s?mode=ro'%db, uri=True)
    try:
        for m,src,n in c.execute("select model, source, count(*) from sessions group by model, source"):
            agg[(prof,m)]+=n
    except Exception: pass
CLOUDY=('qwen3.8-27b','gemma4','kimi','muse','glm-5','minimax','gpt-','seed-2','gemma','qwen3.5','qwen3-vl','nemotron','deepseek-v4.1','mistral-large','gpt-oss')
tot=0
for (prof,m),n in sorted(agg.items(), key=lambda x:-x[1]):
    if m and any(h in m for h in CLOUDY):
        print("  %-20s %-34s %d sessions" % (prof, m[:34], n)); tot+=n
print("  TOTAL cloud-model session rows: %d" % tot)
