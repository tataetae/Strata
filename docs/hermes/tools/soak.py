#!/usr/bin/env python3
"""N concurrent clients, fixed token budget each.

Reports the engine's OWN decode-phase rate (the number the engine records per
request), not a client-side wall clock: wall clock includes the prompt read and
queue time, so it understates the decode rate badly when the batch is busy.

Usage: soak.py [clients] [tokens_each]
"""
import concurrent.futures as cf, json, sys, time, urllib.request

KEY = open('/home/clara/.config/opencode/strata.key').read().strip()
BASE = 'http://127.0.0.1:8080'
PROMPT = ('Continue this list of short factual sentences, one per line:\n'
          '1. The sun rises in the east.\n2.')


def metrics():
    r = urllib.request.Request(BASE + '/metrics',
                               headers={'Authorization': 'Bearer ' + KEY})
    with urllib.request.urlopen(r, timeout=15) as x:
        return json.load(x)


def one(i, tokens):
    body = json.dumps({
        'model': 'qwen3.8-flash-next-q4_k_xl',
        'messages': [{'role': 'user', 'content': PROMPT}],
        'max_tokens': tokens,
        'temperature': 0.6,
        'stream': False,
    }).encode()
    req = urllib.request.Request(BASE + '/v1/chat/completions', data=body,
                                 headers={'Authorization': 'Bearer ' + KEY,
                                          'Content-Type': 'application/json'})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=3600) as r:
            d = json.load(r)
        u = d.get('usage') or {}
        got = u.get('completion_tokens') or tokens
        ok = True
    except Exception as e:
        got, ok = 0, False
        d = str(e)
    return {'i': i, 'ok': ok, 'tokens': got, 'wall_s': round(time.time() - t0, 1)}


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    tokens = int(sys.argv[2]) if len(sys.argv) > 2 else 600
    before = len((metrics().get('requests') or []))
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=n) as ex:
        rows = list(ex.map(lambda i: one(i, tokens), range(n)))
    wall = time.time() - t0
    time.sleep(2)

    m = metrics()
    reqs = m.get('requests') or []
    # the requests that finished during this window: match on output tokens
    mine = [r for r in reqs if (r.get('output_tokens') or 0) > 0][:n]
    rates = [(r.get('decode_tok_s') or 0) for r in mine]
    rates = [x for x in rates if x > 0]
    out = {
        'clients': n,
        'tokens_each': tokens,
        'wall_s': round(wall, 1),
        'client_wall_tok_s': round(sum(r['tokens'] for r in rows) / wall, 1) if wall else 0,
        'engine_per_stream_tok_s': [round(x, 1) for x in rates],
        'engine_aggregate_tok_s': round(sum(rates), 1) if rates else None,
        'engine_aggregate_mean_of_streams': round(sum(rates) / len(rates), 1) if rates else None,
        'faults': sum(0 if r['ok'] else 1 for r in rows),
        'queued_now': (m.get('live') or {}).get('queued'),
        'expert_mib': (m.get('engine') or {}).get('expert_cache_mib'),
    }
    print(json.dumps(out))


if __name__ == '__main__':
    main()
