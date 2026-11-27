#!/usr/bin/env python3
"""The bot chat's wait after a tool call - the final decomposition.

The engine's log line, read correctly:

    prompt N tokens = A reused + B of C read in X ms (rate), G generated in D ms

      N = the whole prompt (the conversation + the tool result)
      A = the prefix the engine reused (the conversation it already had)
      C = the tokens it had to read
      B = the tokens read in the last segment
      X = the wall time of the read

A request whose C is much larger than A is a COLD read: the engine had to
re-read the conversation from the model.  One whose A ~ N is WARM.

The client-side wait (the tool result -> the first chunk) comes from the task
logger's samples: "waiting for stream response (Ns, first_chunk)".
"""
import collections
import glob
import json
import os
import re
import statistics

ELOG = '/home/clara/local-inference/strata-src/strata-iq4xs.log'
PROMPT_RE = re.compile(
    r'prompt (\d+) tokens = (\d+) reused \+ (\d+)(?: of (\d+))? read in (\d+) ms \(([\d.]+) tok/s\), '
    r'(\d+) generated in (\d+) ms \(([\d.]+) tok/s\)(.*)$')
RESTORE_RE = re.compile(r'conversation cache: restored (\d+) tokens \((live|checkpoint)\) in ([\d.]+) ms')
CANCEL_RE = re.compile(r'\(cancelled\)')
WAIT_RE = re.compile(r'waiting for stream response \((\d+)s, (first_chunk|no chunks yet|post_chunk)\)')


def pct(xs, q):
    if not xs:
        return 0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * q))]


def engine_requests():
    out = []
    pending_restore = None
    for line in open(ELOG, errors='replace'):
        m = PROMPT_RE.search(line)
        if m:
            g = m.groups()
            need = int(g[3]) if g[3] else int(g[2])
            out.append(dict(total=int(g[0]), reused=int(g[1]), read=int(g[2]), need=need,
                            read_ms=int(g[4]), gen=int(g[6]), gen_ms=int(g[7]),
                            restore_ms=pending_restore[0] if pending_restore else 0.0,
                            restore_kind=pending_restore[1] if pending_restore else '-',
                            cancelled=bool(CANCEL_RE.search(m.group(9) or ''))))
            pending_restore = None
            continue
        m = RESTORE_RE.search(line)
        if m:
            pending_restore = (float(m.group(3)), m.group(2), int(m.group(1)))
    return out


def task_samples(days):
    out = []
    for f in sorted(glob.glob('/home/clara/.hermes/logs/task-2026*.jsonl')):
        if os.path.basename(f)[5:13] not in days:
            continue
        for l in open(f, errors='replace'):
            try:
                out.append(json.loads(l))
            except Exception:
                pass
    out.sort(key=lambda d: d.get('ts') or 0)
    return out


def main():
    days = set(os.environ.get('DAYS', '20261125,20261126,20261127').split(','))
    eng = engine_requests()
    samples = task_samples(days)

    print('════ 1. the engine requests, split by how much of the prompt it had already ════')
    print('  %-26s %5s %10s %10s %10s %9s %8s' % ('the request', 'n', 'p50 prompt', 'p50 reused',
                                                 'p50 to read', 'p50 read s', 'cancelled'))
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
        print('  %-26s %5d %10d %10d %10d %9.1f %8d'
              % (name, len(g), statistics.median([r['total'] for r in g]),
                 statistics.median([r['reused'] for r in g]),
                 statistics.median([r['need'] for r in g]),
                 statistics.median([r['read_ms'] for r in g]) / 1000.0,
                 sum(1 for r in g if r['cancelled'])))

    print()
    print('════ 2. the read wall time against the tokens actually read ════')
    print('  %-22s %5s %12s %10s %10s %10s' % ('the tokens read', 'n', 'p50 read ms', 'p50 s', 'p90 s', 'p50 tok/s'))
    for lo, hi in [(0, 5000), (5000, 20000), (20000, 100000), (100000, 10 ** 9)]:
        g = [r for r in eng if lo <= r['need'] < hi and not r['cancelled']]
        if len(g) < 3:
            continue
        print('  %-22s %5d %12d %10.1f %10.1f %10.1f'
              % ('%dk-%dk' % (lo // 1000, hi // 1000) if hi < 10 ** 9 else 'over 100k',
                 len(g), statistics.median([r['read_ms'] for r in g]),
                 statistics.median([r['read_ms'] for r in g]) / 1000.0,
                 pct([r['read_ms'] for r in g], .9) / 1000.0,
                 statistics.median([r['need'] * 1000.0 / max(r['read_ms'], 1) for r in g])))

    print()
    print('════ 3. COLD vs WARM: was the conversation still in the engine? ════')
    cold = [r for r in eng if r['total'] > 100000 and r['reused'] < r['total'] * 0.2 and not r['cancelled']]
    warm = [r for r in eng if r['total'] > 100000 and r['reused'] >= r['total'] * 0.8 and not r['cancelled']]
    mid = [r for r in eng if r['total'] > 100000 and r['reused'] < r['total'] * 0.8
           and r['reused'] >= r['total'] * 0.2 and not r['cancelled']]
    for name, g in (('COLD (the engine had <20%)', cold), ('PARTIAL (20-80%)', mid), ('WARM (>80%)', warm)):
        if not g:
            continue
        print('  %-26s the requests %4d | the prompt p50 %7d | the read p50 %7d | the wall p50 %6.1f s | the decode p50 %5.1f s'
              % (name, len(g), statistics.median([r['total'] for r in g]),
                 statistics.median([r['need'] for r in g]),
                 statistics.median([r['read_ms'] for r in g]) / 1000.0,
                 statistics.median([r['gen_ms'] for r in g]) / 1000.0))

    print()
    print('════ 4. the restore (the conversation cache) ════')
    res = [r for r in eng if r['restore_ms'] > 0]
    if res:
        for kind in ('live', 'checkpoint'):
            g = [r['restore_ms'] / 1000.0 for r in res if r['restore_kind'] == kind]
            if g:
                print('  the %-11s restores: %3d | p50 %6.1f s | p90 %6.1f s | max %6.1f s'
                      % (kind, len(g), statistics.median(g), pct(g, .9), max(g)))
        print('  the restores as a share of all the >100k requests: %d of %d'
              % (len([r for r in res if r['total'] > 100000]),
                 len([r for r in eng if r['total'] > 100000])))

    print()
    print('════ 5. the client-side wait, per bot chat ════')
    per = collections.defaultdict(list)
    for d in samples:
        ts = d.get('ts')
        if not ts:
            continue
        eng_s = d.get('engine') or {}
        for s in d.get('sessions') or []:
            if (s.get('title') or '').strip().lower() != 'bot chat':
                continue
            key = '%s/%s' % (s.get('profile'), s.get('id'))
            m = WAIT_RE.search(s.get('last_activity') or '')
            per[key].append((ts, int(m.group(1)) if m else None, eng_s))
    tot_wait = 0
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
        tot_wait += sum(waits)
        killed = sum(1 for w in waits if w >= 830)
        print('  %-46s the waits %3d | p50 %4.0f s | p90 %4.0f s | max %4.0f s | the sum %5.1f h | hit the 900 s watchdog %d'
              % (key, len(waits), statistics.median(waits), pct(waits, .9), max(waits),
                 sum(waits) / 3600, killed))
    print('  %-46s the sum of all the waits: %.1f h over %d days' % ('ALL', tot_wait / 3600, len(days)))


if __name__ == '__main__':
    main()
