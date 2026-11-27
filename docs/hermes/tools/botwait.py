#!/usr/bin/env python3
"""The bot chat's wait time after tool calls - where the seconds actually go.

Three sources are joined:
  1. the task logger's 15 s samples (~/.hermes/logs/task-*.jsonl): the session's
     own phase ("waiting for stream response (Ns, first_chunk)") and the engine's
     live state (per-slot state, prompt_total, prompt_read, elapsed_s)
  2. the same samples' "reads": the engine's prompt-read lines, with their ms and rate
  3. the DBs: the tool-result -> next-assistant gap, per bot chat

The output is the decomposition of one wait: engine read / queue wait / decode /
Hermes overhead.
"""
import collections
import datetime
import glob
import json
import os
import re
import sqlite3
import statistics

LOGS = sorted(glob.glob('/home/clara/.hermes/logs/task-2026*.jsonl'))
DBS = {'ROOT': '/home/clara/.hermes/state.db'}
for p in glob.glob('/home/clara/.hermes/profiles/*/state.db'):
    DBS[os.path.basename(os.path.dirname(p))] = p

WAIT_RE = re.compile(r'waiting for stream response \((\d+)s, (first_chunk|no chunks yet|post_chunk)\)')


def load_samples(days):
    """Every sample of the given days, in order, with the Bot Chat sessions kept."""
    out = []
    for f in LOGS:
        base = os.path.basename(f)[5:13]
        if base not in days:
            continue
        for l in open(f, errors='replace'):
            try:
                d = json.loads(l)
            except Exception:
                continue
            d['_day'] = base
            out.append(d)
    out.sort(key=lambda d: d.get('ts') or 0)
    return out


def bot_sessions(samples):
    """-> {session_key: [ (ts, wait_s, phase, engine_state, slots, reads) ]}"""
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
            per[key].append((ts, int(m.group(1)) if m else None, m.group(2) if m else None,
                             la, eng, d.get('reads') or []))
    return per


def episodes(rows):
    """Contiguous runs where the session is waiting for the stream."""
    eps = []
    cur = []
    for r in rows:
        if r[1] is not None:
            cur.append(r)
        else:
            if cur:
                eps.append(cur)
                cur = []
    if cur:
        eps.append(cur)
    return eps


def pct(xs, q):
    if not xs:
        return 0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * q))]


def main():
    days = os.environ.get('DAYS', '20261125,20261126,20261127').split(',')
    samples = load_samples(days)
    print('  the samples: %d over %s' % (len(samples), days))
    per = bot_sessions(samples)
    print('  the bot chats seen: %d\n' % len(per))

    print('════ 1. the wait itself: "waiting for stream response" per bot chat ════')
    print('  %-46s %6s %7s %7s %7s %7s %7s' % ('session', 'waits', 'p50', 'p75', 'p90', 'max', 'sum_h'))
    grand = []
    for key, rows in sorted(per.items(), key=lambda kv: -sum(r[1] or 0 for r in kv[1])):
        eps = episodes(rows)
        waits = [max(r[1] for r in e) for e in eps]
        if not waits:
            continue
        grand += waits
        print('  %-46s %6d %7.0f %7.0f %7.0f %7.0f %7.2f'
              % (key, len(waits), statistics.median(waits), pct(waits, .75), pct(waits, .90),
                 max(waits), sum(waits) / 3600))
    if grand:
        print('  %-46s %6d %7.0f %7.0f %7.0f %7.0f %7.2f'
              % ('ALL', len(grand), statistics.median(grand), pct(grand, .75), pct(grand, .90),
                 max(grand), sum(grand) / 3600))

    print()
    print('════ 2. what the engine was doing during the wait ════')
    # for every waiting sample, the engine's slot states
    eng_state = collections.Counter()
    reading = 0
    waiting_slot = 0
    idle = 0
    tot = 0
    for key, rows in per.items():
        for ts, w, phase, la, eng, reads in rows:
            if w is None:
                continue
            tot += 1
            st = eng.get('state')
            eng_state[st] += 1
            slots = eng.get('slots') or []
            any_read = any((s.get('state') == 'reading') for s in slots)
            if any_read:
                reading += 1
            elif eng.get('waiting'):
                waiting_slot += 1
            elif eng.get('state') in ('idle', 'unloaded'):
                idle += 1
    print('  the waiting samples: %d' % tot)
    print('  the engine state during them: %s' % dict(eng_state.most_common()))
    print('  at least one slot READING the prompt : %5.1f %%' % (100.0 * reading / max(tot, 1)))
    print('  no slot reading, but requests WAITING : %5.1f %%' % (100.0 * waiting_slot / max(tot, 1)))
    print('  the engine idle                       : %5.1f %%' % (100.0 * idle / max(tot, 1)))

    print()
    print('════ 3. the engine reads that happened inside the wait episodes ════')
    # a read event is timestamped by the sample that saw it; attribute it to the
    # bot chat whose episode was running at that moment
    all_reads = []
    for d in samples:
        for r in d.get('reads') or []:
            all_reads.append((d['ts'], d['_day'], r))
    print('  the engine read events (timestamped): %d' % len(all_reads))
    sizes = [r['total'] for _, _, r in all_reads]
    rates = [r['rate'] for _, _, r in all_reads]
    mss = [r['ms'] / 1000.0 for _, _, r in all_reads]
    if mss:
        print('  the read size  : p50 %d | p90 %d | max %d tokens' % (statistics.median(sizes), pct(sizes, .9), max(sizes)))
        print('  the read time  : p50 %.1f s | p90 %.1f s | max %.1f s' % (statistics.median(mss), pct(mss, .9), max(mss)))
        print('  the read rate  : p50 %.1f | p10 %.1f tok/s' % (statistics.median(rates), pct(rates, .1)))
        big = [r for _, _, r in all_reads if r['read'] > 20000]
        print('  the reads over 20k tokens: %d (%.1f %% of all) and they took %.1f s of the %.1f s total'
              % (len(big), 100.0 * len(big) / len(all_reads),
                 sum(b['ms'] for b in big) / 1000.0, sum(mss)))
        small = [r for _, _, r in all_reads if r['read'] <= 2000]
        if small:
            print('  the reads under 2k tokens : %d, %.1f s total, %.2f s each'
                  % (len(small), sum(b['ms'] for b in small) / 1000.0,
                     sum(b['ms'] for b in small) / 1000.0 / len(small)))

    print()
    print('════ 4. the DB side: the tool result -> next assistant gap ════')
    for key, rows in sorted(per.items()):
        prof, sid = key.split('/', 1)
        db = DBS.get(prof if prof != 'default' else 'ROOT')
        if not db or not os.path.isfile(db):
            continue
        con = sqlite3.connect('file:%s?mode=ro' % db, uri=True)
        q = con.execute("select role, tool_name, timestamp from messages where session_id=? order by timestamp",
                        (sid,)).fetchall()
        q = [(r[0], r[1], float(r[2]) if r[2] else None) for r in q]
        gaps = []
        for i, (role, tool, t) in enumerate(q):
            if role != 'tool' or t is None:
                continue
            for j in range(i + 1, len(q)):
                if q[j][0] == 'assistant' and q[j][2]:
                    gaps.append((t, q[j][2], tool, q[j][2] - t))
                    break
        if not gaps:
            continue
        # only the gaps that fall inside the sampled window
        lo = min(r[0] for r in rows)
        hi = max(r[0] for r in rows)
        g = [x for x in gaps if lo <= x[0] <= hi]
        if not g:
            continue
        gg = [x[3] for x in g]
        print('  %-46s the gaps in the window: %d | p50 %.0f s | p90 %.0f s | max %.0f s | the sum %.1f h'
              % (key, len(gg), statistics.median(gg), pct(gg, .9), max(gg), sum(gg) / 3600))


if __name__ == '__main__':
    main()
