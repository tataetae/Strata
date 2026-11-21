#!/usr/bin/env python
"""Fire the Hermes summariser on the REMAINING bloated bot chats, one at a time."""
import json, subprocess, sys, time, os, sqlite3
sys.path.insert(0, '/tmp/opencode')

BATCH = json.load(open('/tmp/opencode/batch.json'))
LOG = '/tmp/opencode/batch_log.txt'
SKIP = {'20261019_201306_f301bf'}

def log(msg):
    line = "%s  %s" % (time.strftime('%H:%M:%S'), msg)
    print(line, flush=True)
    open(LOG, 'a').write(line + "\n")

def already_done(home, sid):
    dbp = os.path.join(home, 'state.db')
    c = sqlite3.connect('file:%s?mode=ro' % dbp, uri=True)
    n = c.execute("select count(*) from messages where session_id=? and compacted=1", (sid,)).fetchone()[0]
    c.close()
    return n > 0

ok = fail = skip = 0
for i, r in enumerate(BATCH, 1):
    sid = r['session_id']; prof = r['profile']; home = r['home']
    if sid in SKIP:
        log("[%2d/%d] SKIP  %-20s %-24s (live in the gateway)" % (i, len(BATCH), prof, sid[:24])); skip += 1; continue
    if already_done(home, sid):
        log("[%2d/%d] DONE  %-20s %-24s (already compacted)" % (i, len(BATCH), prof, sid[:24])); skip += 1; continue
    t0 = time.time()
    log("[%2d/%d] ----  %-20s %-24s %7d tok -> compacting" % (i, len(BATCH), prof, sid[:24], r['tokens']))
    try:
        p = subprocess.run(['/tmp/opencode/run_compress.sh', home, sid], capture_output=True, text=True, timeout=3600)
        out = (p.stdout or '') + (p.stderr or '')
    except subprocess.TimeoutExpired:
        out = ''; log("      -> TIMEOUT after 3600s")
    status = 'failed'
    for line in out.splitlines():
        if 'status=' in line: status = line.split('status=')[1].split()[0]
        if 'FAILED' in line: status = 'failed'
    dt = time.time() - t0
    if status == 'compressed':
        ok += 1
        log("      -> compressed in %.0fs" % dt)
    else:
        fail += 1
        log("      -> %s in %.0fs" % (status, dt))
        for line in out.splitlines()[-4:]:
            log("         | %s" % line[:160])
    time.sleep(5)
log("DONE: %d compressed, %d failed, %d skipped/already-done" % (ok, fail, skip))
