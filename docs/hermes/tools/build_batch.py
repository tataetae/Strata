import json, sqlite3, glob, os, time, sys
sys.path.insert(0,'/home/clara/local-inference/strata-src/tools')
from strata_tokenizer import Tokenizer
D='/srv/inference/models/qwen3.8-flash-next/packs/iq4_xs/tokenizer'
ids=json.load(open(os.path.join(D,'vocab.json')))
tokens=[None]*len(ids)
for t,i in ids.items(): tokens[i]=t
merges=[l.rstrip('\n') for l in open(os.path.join(D,'merges.txt'))]
types=json.load(open(os.path.join(D,'token_type.json')))
tk=Tokenizer(tokens, merges, types, pre='qwen35')

# per-profile thresholds
THRESH={'ROOT':150000}
def thresh(prof, cfg):
    t = cfg.get('compression') or {}
    return int(t.get('threshold_tokens') or 150000)

import yaml
SRC=('bot_room','gui','tui','desktop','webui','cli','line','api_server')
rows=[]
for db in sorted(glob.glob('/home/clara/.hermes/state.db')+glob.glob('/home/clara/.hermes/profiles/*/state.db')):
    prof='ROOT' if db.endswith('/.hermes/state.db') else db.split('/profiles/')[1].split('/')[0]
    home='/home/clara/.hermes' if prof=='ROOT' else '/home/clara/.hermes/profiles/'+prof
    try:
        cfg=yaml.safe_load(open(os.path.join(home,'config.yaml'))) or {}
    except Exception:
        cfg={}
    thr=thresh(prof,cfg)
    try:
        c=sqlite3.connect('file:%s?mode=ro'%db, uri=True)
        r=c.execute("""select s.id, s.source, s.model, s.last_activity_at, s.archived,
             (select group_concat(coalesce(m.content,''),'\\n') from messages m where m.session_id=s.id and m.active=1)
             from sessions s where s.source in (?,?,?,?,?,?,?,?) and s.archived=0 and s.ended_at is null""", SRC).fetchall()
    except Exception:
        continue
    for sid, src, model, la, arch, blob in r:
        if not blob: continue
        rows.append((prof, home, sid, src, model, la or 0, thr, len(blob), blob))

print("  tokenising %d live chat sessions..." % len(rows), flush=True)
out=[]
for prof, home, sid, src, model, la, thr, blen, blob in rows:
    if blen < 120000:  # <120KB cannot exceed ~40k tokens; skip the expensive tokenise
        continue
    nt = len(tk.encode(blob, parse_special=False))
    if nt > thr:
        out.append({'profile':prof,'home':home,'session_id':sid,'source':src,'model':model,
                    'tokens':nt,'threshold':thr,'age_h':(time.time()-la)/3600})
out.sort(key=lambda r: -r['tokens'])
json.dump(out, open('/tmp/opencode/batch.json','w'), indent=1)
print("  %d sessions over their profile's threshold:" % len(out))
print("  %8s %8s %-20s %-24s %-8s %-26s %7s" % ('TOKENS','THRESH','profile','session_id','source','model','age_h'))
for r in out:
    print("  %8d %8d %-20s %-24s %-8s %-26s %7.1f" % (r['tokens'], r['threshold'], r['profile'], r['session_id'][:24], r['source'], (r['model'] or '')[:26], r['age_h']))
