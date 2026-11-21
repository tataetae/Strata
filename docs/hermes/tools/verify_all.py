import json, sqlite3, glob, os, sys, time
sys.path.insert(0,'/home/clara/local-inference/strata-src/tools')
from strata_tokenizer import Tokenizer
D='/srv/inference/models/qwen3.8-flash-next/packs/iq4_xs/tokenizer'
ids=json.load(open(os.path.join(D,'vocab.json'))); toks=[None]*len(ids)
for t,i in ids.items(): toks[i]=t
mg=[l.rstrip('\n') for l in open(os.path.join(D,'merges.txt'))]
ty=json.load(open(os.path.join(D,'token_type.json')))
tk=Tokenizer(toks,mg,ty,pre='qwen35')
batch=json.load(open('/tmp/opencode/batch.json'))
tot_before=tot_after=0; done=0
print("  %-20s %-22s %9s %9s %9s" % ('profile','session_id','before','after','saved'))
for r in batch:
    c=sqlite3.connect('file:%s?mode=ro'%os.path.join(r['home'],'state.db'), uri=True)
    comp=c.execute("select count(*) from messages where session_id=? and compacted=1",(r['session_id'],)).fetchone()[0]
    blob=c.execute("select group_concat(coalesce(content,''),'\\n') from messages where session_id=? and active=1",(r['session_id'],)).fetchone()[0]
    c.close()
    if not blob: continue
    nt=len(tk.encode(blob, parse_special=False))
    if comp>0 and nt < r['tokens']*0.9:
        done+=1; tot_before+=r['tokens']; tot_after+=nt
        print("  %-20s %-22s %9d %9d %9d" % (r['profile'], r['session_id'][:22], r['tokens'], nt, r['tokens']-nt))
print("  --- compacted: %d of %d | tokens %d -> %d (saved %d)" % (done, len(batch), tot_before, tot_after, tot_before-tot_after))
