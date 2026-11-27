#!/usr/bin/env python3
"""The bot chat's wait after a tool call - the definitive decomposition.

Per request the engine logs:
    strata batch: read N tokens on a thread of its own in X ms (concurrent, to M of T)
    strata serve: prompt T tokens = A reused + B of C read in Y ms (rate), G generated in D ms

  T = the whole prompt (the conversation + the tool result)
  A = the prefix the engine reused
  C = the tokens it had to read (A + C = T)
  the batch lines = the chunks it read, each with its own time
  the sum of the chunk times = the wall time of the read

The client-side wait (the tool result -> the first chunk) is the task logger's
"waiting for stream response (Ns, first_chunk)".
"""
import collections
import glob
import json
import os
import re
import statistics

ELOG = '/home/clara/local-inference/strata-src/strata-iq4xs.log'
CHUNK_RE = re.compile(r'strata batch: read (\d+) tokens on a thread of its own in (\d+) ms \(concurrent, to (\d+) of (\d+)\)')
PROMPT_RE = re.compile(
    r'prompt (\d+) tokens = (\d+) reused \+ (\d+)(?: of (\d+))? read in (\d+) ms \(([\d.]+) tok/s\), '
    r'(\d+) generated in (\d+) ms \(([\d.]+) tok/s\)')
GIVEWAY_RE = re.compile(r'the prompt read gives way at (\d+) of (\d+) tokens')
RESTORE_RE = re.compile(r'conversation cache: restored (\d+) tokens \((live|checkpoint)\) in ([\d.]+) ms')
WAIT_RE = re.compile(r'waiting for stream response \((\d+)s, (first_chunk|no chunks yet|post_chunk)\)')


def pct(xs, q):
    if not xs:
        return 0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * q))]


def engine_requests():
    """Walk the log; each 'prompt ...' line closes one request."""
    out = []
    chunks = []          # the chunk reads of the request being assembled
    restore = None
    gave_way = False
    for line in open(ELOG, errors='replace'):
        m = CHUNK_RE.search(line)
        if m:
            chunks.append((int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))))
            continue
        if GIVEWAY_RE.search(line):
            gave_way = True
            continue
        m = RESTORE_RE.search(line)
        if m:
            restore = (float(m.group(3)), m.group(2), int(m.group(1)))
            continue
        m = PROMPT_RE.search(line)
        if m:
            g = m.groups()
            need = int(g[3]) if g[3] else int(g[2])
            out.append(dict(total=int(g[0]), reused=int(g[1]), need=need,
                            wall_ms=int(g[4]), gen=int(g[6]), gen_ms=int(g[7]),
                            chunks=list(chunks), chunk_ms=sum(c[1] for c in chunks),
                            chunk_tok=sum(c[0] for c in chunks), n_chunks=len(chunks),
                            restore_ms=restore[0] if restore else 0.0,
                            restore_kind=restore[1] if restore else '-',
                            gave_way=gave_way, cancelled='(cancelled)' in line))
            chunks = []
            restore = None
            gave_way = False
    return out


def main():
    eng = engine_requests()
    print('  the engine requests in the log: %d' % len(eng))

    print()
    print('════ 1. the chunk read: how fast does the engine eat the prompt? ════')
    allch = []
    for r in eng:
        for c in r['chunks']:
            allch.append((c[0], c[1], r['total']))
    print('  the chunk reads: %d' % len(allch))
    print('  %-24s %6s %10s %10s %10s' % ('the chunk size', 'n', 'p50 ms', 'p90 ms', 'p50 tok/s'))
    for lo, hi in [(0, 3000), (3000, 6000), (6000, 10000), (10000, 10 ** 9)]:
        g = [c for c in allch if lo <= c[0] < hi]
        if len(g) < 3:
            continue
        print('  %-24s %6d %10d %10d %10.1f'
              % ('%dk-%dk' % (lo // 1000, hi // 1000) if hi < 10 ** 9 else 'over 10k',
                 len(g), statistics.median([c[1] for c in g]), pct([c[1] for c in g], .9),
                 statistics.median([c[0] * 1000.0 / c[1] for c in g])))

    print()
    print('════ 2. the requests, split by how much the engine already had ════')
    print('  %-30s %5s %10s %10s %8s %8s %8s' % ('the request', 'n', 'p50 prompt', 'p50 read',
                                                 'p50 chk', 'p50 wall', 'gave way'))
    groups = [
        ('the prompt < 20k', lambda r: r['total'] < 20000),
        ('20k-100k', lambda r: 20000 <= r['total'] < 100000),
        ('100k-250k', lambda r: 100000 <= r['total'] < 250000),
        ('250k and over', lambda r: r['total'] >= 250000),
    ]
    for name, f in groups:
        g = [r for r in eng if f(r)]
        if not g:
            continue
        print('  %-30s %5d %10d %10d %8.1f %8.1f %8d'
              % (name, len(g), statistics.median([r['total'] for r in g]),
                 statistics.median([r['need'] for r in g]),
                 statistics.median([r['n_chunks'] for r in g]),
                 statistics.median([r['wall_ms'] for r in g]) / 1000.0,
                 sum(1 for r in g if r['gave_way'])))

    print()
    print('════ 3. the >100k prompts: COLD (the engine had to re-read) vs WARM ════')
    big = [r for r in eng if r['total'] > 100000]
    cold = [r for r in big if r['reused'] < r['total'] * 0.2]
    warm = [r for r in big if r['reused'] >= r['total'] * 0.8]
    for name, g in (('COLD (reused < 20%)', cold), ('WARM (reused > 80%)', warm)):
        if not g:
            continue
        walls = [r['wall_ms'] / 1000.0 for r in g]
        chks = [r['n_chunks'] for r in g]
        need = [r['need'] for r in g]
        print('  %-22s the requests %4d | the prompt p50 %7d | the read p50 %7d | the chunks p50 %3.0f | the wall p50 %6.1f s | p90 %6.1f s'
              % (name, len(g), statistics.median([r['total'] for r in g]), statistics.median(need),
                 statistics.median(chks), statistics.median(walls), pct(walls, .9)))
        print('  %-22s the chunks that could not be read (the read gave way): %d of %d'
              % ('', sum(1 for r in g if r['gave_way']), len(g)))

    print()
    print('════ 4. the restore (the conversation cache) ════')
    res = [r for r in eng if r['restore_ms'] > 0]
    for kind in ('live', 'checkpoint'):
        g = [r['restore_ms'] / 1000.0 for r in res if r['restore_kind'] == kind]
        if g:
            print('  the %-11s restores: %4d | p50 %6.1f s | p90 %6.1f s | max %6.1f s'
                  % (kind, len(g), statistics.median(g), pct(g, .9), max(g)))
    rb = [r for r in res if r['total'] > 100000]
    print('  the >100k requests with a restore: %d of %d (%.0f %%)'
          % (len(rb), len(big), 100.0 * len(rb) / max(len(big), 1)))
    if rb:
        print('  the >100k requests with NO restore: %d' % (len(big) - len(rb)))

    print()
    print('════ 5. the client-side wait, and the engine\'s share of it ════')
    days = set(os.environ.get('DAYS', '20261125,20261126,20261127').split(','))
    samples = []
    for f in sorted(glob.glob('/home/clara/.hermes/logs/task-2026*.jsonl')):
        if os.path.basename(f)[5:13] not in days:
            continue
        for l in open(f, errors='replace'):
            try:
                samples.append(json.loads(l))
            except Exception:
                pass
    samples.sort(key=lambda d: d.get('ts') or 0)
    per = collections.defaultdict(list)
    for d in samples:
        ts = d.get('ts')
        if not ts:
            continue
        for s in d.get('sessions') or []:
            if (s.get('title') or '').strip().lower() != 'bot chat':
                continue
            m = WAIT_RE.search(s.get('last_activity') or '')
            per['%s/%s' % (s.get('profile'), s.get('id'))].append((ts, int(m.group(1)) if m else None))
    tot = 0
    kills = 0
    for key, rows in sorted(per.items(), key=lambda kv: -sum(r[1] or 0 for r in kv[1])):
        eps, cur = [], []
        for r in rows:
            if r[1] is not None:
                cur.append(r)
            else:
                if cur:
                    eps.append(cur)
                    cur = []
        if cur:
            eps.append(cur)
        waits = [max(r[1] for r in e) for e in eps]
        if not waits:
            continue
        tot += sum(waits)
        kills += sum(1 for w in waits if w >= 830)
        print('  %-46s the waits %3d | p50 %4.0f s | p90 %4.0f s | max %4.0f s | the sum %5.1f h | the 900 s watchdog %d'
              % (key, len(waits), statistics.median(waits), pct(waits, .9), max(waits),
                 sum(waits) / 3600, sum(1 for w in waits if w >= 830)))
    print('  %-46s the sum %5.1f h | the watchdog kills %d' % ('ALL', tot / 3600, kills))


if __name__ == '__main__':
    main()
