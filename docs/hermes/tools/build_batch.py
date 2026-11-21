import json, sqlite3, glob, os, sys
sys.path.insert(0, '/home/clara/local-inference/strata-src/tools')
from strata_tokenizer import Tokenizer
D = '/srv/inference/models/qwen3.8-flash-next/packs/iq4_xs/tokenizer'
ids = json.load(open(os.path.join(D, 'vocab.json'))); toks = [None]*len(ids)
for t, i in ids.items(): toks[i] = t
mg = [l.rstrip('\n') for l in open(os.path.join(D, 'merges.txt'))]
ty = json.load(open(os.path.join(D, 'token_type.json')))
tk = Tokenizer(toks, mg, ty, pre='qwen35')
out = []
for db in sorted(glob.glob('/home/clara/.hermes/state.db') + glob.glob('/home/clara/.hermes/profiles/*/state.db')):
    prof = 'ROOT' if db.endswith('/.hermes/state.db') else db.split('/profiles/')[1].split('/')[0]
    home = os.path.dirname(db)
    c = sqlite3.connect('file:%s?mode=ro' % db, uri=True)
    for sid, blob in c.execute("""select s.id, (select group_concat(coalesce(m.content,''),'\\n') from messages m where m.session_id=s.id and m.active=1)
                                   from sessions s where s.archived=0 and s.ended_at is null"""):
        if not blob or len(blob) < 150000: continue
        nt = len(tk.encode(blob, parse_special=False))
        if nt > 150000:
            comp = c.execute("select count(*) from messages where session_id=? and compacted=1", (sid,)).fetchone()[0]
            out.append({'profile': prof, 'home': home, 'session_id': sid, 'tokens': nt, 'compacted': comp})
    c.close()
json.dump(out, open('/tmp/opencode/batch.json', 'w'), indent=1)
print("  %d live chats over 150k tokens (%d already compacted)" % (len(out), sum(1 for r in out if r['compacted'])))
for r in sorted(out, key=lambda x: -x['tokens'])[:16]:
    print("    %-24s %8d tok  compacted=%-6d %s" % (r['session_id'][:24], r['tokens'], r['compacted'],
          '' if r['compacted'] else '<-- NEVER COMPRESSED'))
