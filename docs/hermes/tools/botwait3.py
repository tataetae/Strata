#!/usr/bin/env python3
"""Join the engine's log with the task logger: the full decomposition of one
"wait after a tool call".

The engine prints, per request:

    conversation cache: restored N tokens (live|checkpoint) in X ms
    prompt N tokens = A reused + B read in Y ms (R tok/s), G generated in D ms (E tok/s)

The task logger timestamps those read lines.  Matching on (total, reused, read)
gives every request a wall-clock time and the restore that went with it.
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
    r'(\d+) generated in (\d+) ms \(([\d.]+) tok/s\)')
RESTORE_RE = re.compile(r'conversation cache: restored (\d+) tokens \((live|checkpoint)\) in ([\d.]+) ms')
PARK_RE = re.compile(r'conversation cache: parked (\d+) tokens in ([\d.]+) ms')


def pct(xs, q):
    if not xs:
        return 0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * q))]


def engine_requests():
    """(total, reused, read, read_ms, rate, gen, gen_ms, gen_rate, restore_ms, restore_kind, restore_tok)"""
    out = []
    pending_restore = None
    for line in open(ELOG, errors='replace'):
        m = PROMPT_RE.search(line)
        if m:
            g = m.groups()
            out.append(dict(total=int(g[0]), reused=int(g[1]), read=int(g[2]),
                            read_ms=int(g[4]), rate=float(g[5]),
                            gen=int(g[6]), gen_ms=int(g[7]), gen_rate=float(g[8]),
                            restore_ms=pending_restore[0] if pending_restore else 0.0,
                            restore_kind=pending_restore[1] if pending_restore else '-',
                            restore_tok=pending_restore[2] if pending_restore else 0))
            pending_restore = None
            continue
        m = RESTORE_RE.search(line)
        if m:
            pending_restore = (float(m.group(3)), m.group(2), int(m.group(1)))
    return out


def task_reads(days):
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
    out = []
    for d in samples:
        if not d.get('ts'):
            continue
        for r in d.get('reads') or []:
            out.append((d['ts'], r))
    return out


def main():
    days = set(os.environ.get('DAYS', '20261125,20261126,20261127').split(','))
    eng = engine_requests()
    tr = task_reads(days)
    print('  the engine request lines: %d | the timestamped reads: %d' % (len(eng), len(tr)))

    # index the engine requests by (total, reused, read)
    idx = collections.defaultdict(list)
    for r in eng:
        idx[(r['total'], r['reused'], r['read'])].append(r)

    joined = []
    used = collections.Counter()
    for ts, r in tr:
        key = (r['total'], r['reused'], r['read'])
        lst = idx.get(key)
        if not lst:
            continue
        # take the earliest unused match (the engine lines are chronological)
        pick = None
        for cand in lst:
            if used[cand['total'], cand['reused'], cand['read'], id(cand)] == 0 and cand.get('_used') is None:
                pick = cand
                break
        if pick is None:
            continue
        pick['_used'] = True
        j = dict(pick)
        j['ts'] = ts
        j['read_ms_log'] = pick['read_ms']
        joined.append(j)
    print('  the joined requests: %d' % len(joined))

    big = [j for j in joined if j['total'] > 100000]
    print()
    print('════ the requests over 100k tokens (the bot chats) ════')
    print('  %6s %9s %9s %8s %8s %8s %8s %8s' % ('total', 'reused', 'read', 'read s', 'restore s',
                                                'restore k', 'gen s', 'read tok/s'))
    for j in joined[-40:]:
        print('  %6d %9d %9d %8.1f %8.1f %8s %8.1f %8.1f'
              % (j['total'], j['reused'], j['read'], j['read_ms'] / 1000.0, j['restore_ms'] / 1000.0,
                 j['restore_kind'], j['gen_ms'] / 1000.0, j['rate']))

    print()
    print('════ the decomposition, for the requests over 100k tokens ════')
    if big:
        tot = sum(j['read_ms'] + j['gen_ms'] + j['restore_ms'] for j in big) / 1000.0
        rs = sum(j['read_ms'] for j in big) / 1000.0
        gs = sum(j['gen_ms'] for j in big) / 1000.0
        ts_ = sum(j['restore_ms'] for j in big) / 1000.0
        print('  the requests: %d | the engine time: %.0f s' % (len(big), tot))
        print('  the prompt read : %7.0f s  (%4.1f %%)' % (rs, 100 * rs / max(tot, 1)))
        print('  the restore     : %7.0f s  (%4.1f %%)' % (ts_, 100 * ts_ / max(tot, 1)))
        print('  the decode      : %7.0f s  (%4.1f %%)' % (gs, 100 * gs / max(tot, 1)))
        rest = [j['restore_ms'] / 1000.0 for j in big if j['restore_ms'] > 0]
        if rest:
            print('  the restores: %d | p50 %.1f s | p90 %.1f s | max %.1f s'
                  % (len(rest), statistics.median(rest), pct(rest, .9), max(rest)))
        cold = [j for j in big if j['reused'] == 0 and j['read'] > 50000]
        warm = [j for j in big if j['reused'] > 0 and j['read'] < 50000]
        if cold:
            print('  the COLD requests (no prefix reuse, read >50k): %d | p50 read %.0f s'
                  % (len(cold), statistics.median([j['read_ms'] / 1000.0 for j in cold])))
        if warm:
            print('  the WARM requests (prefix reused): %d | p50 read %.0f s | p50 total %.0f s'
                  % (len(warm), statistics.median([j['read_ms'] / 1000.0 for j in warm]),
                     statistics.median([(j['read_ms'] + j['gen_ms']) / 1000.0 for j in warm])))


if __name__ == '__main__':
    main()
