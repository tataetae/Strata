#!/usr/bin/env python3
"""Measure the engine's prefill + decode on a quiet box.

One 24k-token cold read + 200 tokens of decode, then one 62k-token cold read
(the owner's real prompt size), taking the engine's own numbers from the log.
"""
import json
import os
import re
import sys
import time
import urllib.request

API = 'http://127.0.0.1:8080/v1/chat/completions'
KEY = open('/home/clara/.config/opencode/strata.key').read().strip()
FILLER = ('The quick brown fox jumps over the lazy dog while the night shift operator '
          'watches the coolant temperature trend and logs the reading.')


def build_prompt(nonce, lines=800):
    parts = ['You are a systems engineer. Read the log below and reply with three '
             'sentences about what stands out.\n']
    for i in range(lines):
        parts.append('log %06d nonce %s: %s\n' % (i, nonce, FILLER))
    return ''.join(parts)


def post(prompt, n_tokens):
    body = json.dumps({'model': 'qwen3.8-flash-next-iq4_xs',
                       'messages': [{'role': 'user', 'content': prompt}],
                       'max_tokens': n_tokens, 'temperature': 0.6,
                       'reasoning_effort': 'none'}).encode()
    req = urllib.request.Request(API, data=body, method='POST',
                                 headers={'Content-Type': 'application/json',
                                          'Authorization': 'Bearer ' + KEY})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=1800) as r:
        d = json.loads(r.read().decode())
    u = d.get('usage') or {}
    return {'wall': time.time() - t0, 'prompt_tokens': u.get('prompt_tokens'),
            'completion_tokens': u.get('completion_tokens')}

LOG = '/home/clara/local-inference/strata-src/strata-iq4xs.log'


def session_start():
    n = 0
    for line in open(LOG, errors='replace'):
        if 'engine started:' in line:
            n += 1
    return n


def measure(n_lines, out_prompt, decode=200):
    before = os.path.getsize(LOG)
    t0 = time.time()
    r = post(build_prompt(out_prompt, n_lines), decode)
    wall = time.time() - t0
    time.sleep(2)
    with open(LOG, errors='replace') as f:
        f.seek(before)
        seg = f.read()
    reads = [float(x) for x in re.findall(r'read in \d+ ms \(([\d.]+) tok/s\)', seg)]
    gens = [float(x) for x in re.findall(r'generated in \d+ ms \(([\d.]+) tok/s\)', seg)]
    pr = re.findall(r'prompt (\d+) tokens = (\d+) reused \+ (\d+)(?: of (\d+))? read in (\d+) ms \(([\d.]+) tok/s\)', seg)
    out = dict(prompt_tokens=r.get('prompt_tokens'), completion_tokens=r.get('completion_tokens'),
                wall=round(wall, 1), reads=[round(x) for x in reads],
                gen_tok_s=round(gens[-1], 1) if gens else 0,
                legs=pr[-3:], nan=seg.count('non-finite logits'))
    return out


if __name__ == '__main__':
    label = sys.argv[1] if len(sys.argv) > 1 else 'run'
    print('════ %s ════' % label, flush=True)
    for name, n in (('the 24k read', 800), ('the 62k read', 2050)):
        m = measure(n, '%s_%s' % (label, name.split()[1]))
        print('  %-14s prompt %6s tok | read %-22s | decode %5.1f tok/s | the wall %6.1f s | NaN %d'
              % (name, m['prompt_tokens'], m['reads'], m['gen_tok_s'], m['wall'], m['nan']), flush=True)
