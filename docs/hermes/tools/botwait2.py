#!/usr/bin/env python3
"""The bot chat's wait after a tool call, decomposed.

The chain is:
  tool result -> Hermes rebuilds the prompt -> the request is queued -> the engine
  admits it -> restores the parked conversation -> reads the prompt -> decodes ->
  the first chunk reaches Hermes.

Every step is measured here:
  * the DB gives the tool-result -> next-assistant gap
  * the task logger gives the client-side wait ("waiting for stream response (Ns)")
    and the engine's live per-slot progress
  * the engine's own log gives the read time, the read rate, the restore time and
    the decode time - timestamped by the task logger's samples
"""
import collections
import glob
import json
import os
import re
import sqlite3
import statistics
import sys

TASK = sorted(glob.glob('/home/clara/.hermes/logs/task-2026*.jsonl'))
DBS = {'ROOT': '/home/clara/.hermes/state.db'}
for p in glob.glob('/home/clara/.hermes/profiles/*/state.db'):
    DBS[os.path.basename(os.path.dirname(p))] = p

WAIT_RE = re.compile(r'waiting for stream response \((\d+)s, (first_chunk|no chunks yet|post_chunk)\)')


def pct(xs, q):
    if not xs:
        return 0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * q))]


def load(days):
    out = []
    for f in TASK:
        if os.path.basename(f)[5:13] not in days:
            continue
        for l in open(f, errors='replace'):
            try:
                d = json.loads(l)
            except Exception:
                continue
            out.append(d)
    out.sort(key=lambda d: d.get('ts') or 0)
    return out


def main():
    days = set(os.environ.get('DAYS', '20261125,20261126,20261127').split(','))
    samples = load(days)
    print('  the samples: %d' % len(samples))

    # ---- 1. the engine's read events, timestamped, with the concurrency at that moment
    reads = []
    for d in samples:
        ts = d.get('ts')
        if not ts:
            continue
        eng = d.get('engine') or {}
        slots = eng.get('slots') or []
        n_reading = sum(1 for s in slots if s.get('state') == 'reading')
        n_slots = len(slots)
        running = eng.get('running') or 0
        for r in d.get('reads') or []:
            reads.append(dict(ts=ts, total=r['total'], reused=r['reused'], read=r['read'],
                               ms=r['ms'], rate=r['rate'], n_reading=n_reading,
                               n_slots=n_slots, running=running))
    # the engine writes the read line when the request ends; the sample that saw it
    # is up to 15 s late, which is fine at these scales.

    print()
    print('════ A. the engine reads, split by the size of the prompt ════')
    buckets = [('under 2k', 0, 2000), ('2k-20k', 2000, 20000), ('20k-100k', 20000, 100000),
               ('100k-200k', 100000, 200000), ('over 200k', 200000, 10 ** 9)]
    print('  %-12s %6s %9s %9s %9s %9s %9s' % ('the read', 'n', 'p50 size', 'p50 s', 'p90 s', 'p50 tok/s', 'p10 tok/s'))
    for name, lo, hi in buckets:
        g = [r for r in reads if lo <= r['read'] < hi]
        if not g:
            continue
        print('  %-12s %6d %9d %9.1f %9.1f %9.1f %9.1f'
              % (name, len(g), statistics.median([r['read'] for r in g]),
                 statistics.median([r['ms'] for r in g]) / 1000.0,
                 pct([r['ms'] for r in g], .9) / 1000.0,
                 statistics.median([r['rate'] for r in g]), pct([r['rate'] for r in g], .1)))

    print()
    print('════ B. the read rate against the concurrency (why 254 and not 520 tok/s) ════')
    print('  %-10s %6s %11s %11s %11s' % ('the reading', 'n', 'p50 tok/s', 'p10 tok/s', 'p50 read s'))
    for n in sorted(set(r['n_reading'] for r in reads)):
        g = [r for r in reads if r['n_reading'] == n and r['read'] > 2000]
        if len(g) < 3:
            continue
        print('  %-10s %6d %11.1f %11.1f %11.1f'
              % (n, len(g), statistics.median([r['rate'] for r in g]),
                 pct([r['rate'] for r in g], .1), statistics.median([r['ms'] for r in g]) / 1000.0))

    print()
    print('════ C. the bot chats: the wait after a tool call ════')
    per = collections.defaultdict(list)
    for d in samples:
        ts = d.get('ts')
        if not ts:
            continue
        eng = d.get('engine') or {}
        for s in d.get('sessions') or []:
            if (s.get('title') or '').strip().lower() != 'bot chat':
                continue
            key = '%s/%s' % (s.get('profile'), s.get('id'))
            la = s.get('last_activity') or ''
            m = WAIT_RE.search(la)
            per[key].append((ts, int(m.group(1)) if m else None, eng, la))
    for key, rows in sorted(per.items(), key=lambda kv: -sum(r[1] or 0 for r in kv[1])):
        # the wait episodes: contiguous runs of "waiting for stream response"
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
        if not eps:
            continue
        waits = [max(r[1] for r in e) for e in eps]
        # how long each episode's engine kept reading
        read_share = []
        for e in eps:
            dur = e[-1][0] - e[0][0]
            rd = sum(1 for r in e if any(s.get('state') == 'reading' for s in (r[2].get('slots') or [])))
            read_share.append(100.0 * rd / max(len(e), 1))
        print('  %-46s the waits %3d | p50 %4.0f s | p90 %4.0f s | max %4.0f s | the sum %5.1f h | reading in %.0f %% of the wait'
              % (key, len(waits), statistics.median(waits), pct(waits, .9), max(waits),
                 sum(waits) / 3600, statistics.mean(read_share)))


if __name__ == '__main__':
    main()
