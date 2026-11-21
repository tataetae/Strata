#!/usr/bin/env python3
"""Cold-prefill prefill-rate harness.

Sends one request with a nonce-derived body so nothing can be reused from the
prompt cache, and reports the engine's own "fresh tokens per second" figure
from the serve log rather than a client-side wall clock.
"""
import json, os, random, string, sys, time, urllib.request

KEY = open('/home/clara/.config/opencode/strata.key').read().strip()
BASE = 'http://127.0.0.1:8080'
LOG = '/home/clara/local-inference/strata-src/strata-iq3_xxs.log'


def nonce_prompt(tokens):
    rnd = random.Random(time.time())
    words = []
    for _ in range(tokens):
        words.append(''.join(rnd.choice(string.ascii_lowercase) for _ in range(rnd.randint(3, 8))))
    return ' '.join(words)


def logsize():
    try:
        return os.path.getsize(LOG)
    except OSError:
        return 0


def tail_from(off):
    try:
        with open(LOG, 'rb') as f:
            f.seek(off)
            return f.read().decode('utf-8', 'replace')
    except OSError:
        return ''


def main():
    tokens = int(sys.argv[1]) if len(sys.argv) > 1 else 16384
    prompt = nonce_prompt(tokens)
    body = json.dumps({
        'model': 'qwen3.8-flash-next-q4_k_xl',
        'messages': [{'role': 'user', 'content': prompt}],
        'max_tokens': 8,
        'temperature': 0.0,
        'stream': False,
    }).encode()
    req = urllib.request.Request(BASE + '/v1/chat/completions', data=body,
                                 headers={'Authorization': 'Bearer ' + KEY,
                                          'Content-Type': 'application/json'})
    off = logsize()
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=1800) as r:
            out = json.load(r)
        ok = True
    except Exception as e:
        out, ok = str(e), False
    wall = time.time() - t0
    time.sleep(1.0)
    txt = tail_from(off)
    fresh = None
    for line in txt.splitlines():
        if 'read in' in line and 'reused' in line:
            # "prompt N tokens = R reused + F read in M ms (X tok/s)"
            try:
                seg = line.split('read in', 1)[1]
                fresh = float(seg.split('(')[1].split('tok/s')[0].strip())
            except Exception:
                pass
    print(json.dumps({'requested_tokens': tokens, 'ok': ok, 'wall_s': round(wall, 1),
                      'fresh_tok_s': fresh}))


if __name__ == '__main__':
    main()
