#!/usr/bin/env python3
"""Re-measure the card order on a quiet box, with the current shape (k8v4, 512K ctx).

For each order: patch the profile, restart the engine, measure two cold reads and
one decode, then record. The card order decides which link the FIRST stage
streams its expert misses over, and the prefill is bound by exactly that.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

PROFILE = '/home/clara/local-inference/strata-src/strata-iq4xs.json'
LOG = '/home/clara/local-inference/strata-src/strata-iq4xs.log'
OUT = '/tmp/opencode/card_order_results.jsonl'
API = 'http://127.0.0.1:8080/v1/chat/completions'
KEY = open('/home/clara/.config/opencode/strata.key').read().strip()
FILLER = ('The quick brown fox jumps over the lazy dog while the night shift operator '
          'watches the coolant temperature trend and logs the reading.')

# the bus score of every card: the NEGOTIATED link (the max width is 16 on all of
# them, so the max says nothing)
def bus_scores():
    out = {}
    for i in range(8):
        try:
            s = subprocess.run(['nvidia-smi', '-i', str(i), '--query-gpu=pcie.link.gen.current,'
                                'pcie.link.width.current', '--format=csv,noheader,nounits'],
                               capture_output=True, text=True, timeout=10).stdout.strip()
            if not s:
                continue
            g, w = (x.strip() for x in s.split(','))
            out[i] = int(g) * int(w)
        except Exception:
            pass
    return out


ORDERS = {
    'A_current_1320': [1, 3, 2, 0],
    'B_link_2310': [2, 3, 1, 0],
    'C_3210': [3, 2, 1, 0],
    'D_link_0213': [0, 2, 1, 3],
}


def build_prompt(nonce, lines=800):
    parts = ['You are a systems engineer. Read the log below and reply with three '
             'sentences about what stands out.\n']
    for i in range(lines):
        parts.append('log %06d nonce %s: %s\n' % (i, nonce, FILLER))
    return ''.join(parts)


def post(prompt, n_tokens=200):
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


def wait_ready(timeout=900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(6)
        try:
            body = json.dumps({'model': 'qwen3.8-flash-next-iq4_xs',
                               'messages': [{'role': 'user', 'content': 'hi'}],
                               'max_tokens': 4, 'reasoning_effort': 'none'}).encode()
            r = urllib.request.Request(API, data=body, method='POST',
                                       headers={'Content-Type': 'application/json',
                                                'Authorization': 'Bearer ' + KEY})
            urllib.request.urlopen(r, timeout=200).read()
            return time.time() - t0
        except Exception:
            pass
    raise SystemExit('the engine did not come back')


def restart():
    subprocess.run(['pkill', '-f', 'strata[.]py serve'], capture_output=True)
    return wait_ready()


def measure(label, lines):
    before = os.path.getsize(LOG)
    r = post(build_prompt(label, lines))
    time.sleep(2)
    with open(LOG, errors='replace') as f:
        f.seek(before)
        seg = f.read()
    reads = [float(x) for x in re.findall(r'read in \d+ ms \(([\d.]+) tok/s\)', seg)]
    gens = [float(x) for x in re.findall(r'generated in \d+ ms \(([\d.]+) tok/s\)', seg)]
    return dict(reads=[round(x) for x in reads],
                decode=round(gens[-1], 1) if gens else 0,
                prompt_tokens=r['prompt_tokens'], wall=round(r['wall'], 1),
                nan=seg.count('non-finite logits'))


if __name__ == '__main__':
    names = sys.argv[1:] or list(ORDERS)
    scores = bus_scores()
    print('  the negotiated bus scores: %s' % scores, flush=True)
    for name in names:
        order = ORDERS[name]
        d = json.load(open(PROFILE))
        d['gpu'] = order
        d['gpu_roles'] = {c: ('main' if c == order[-1] else 'helper') for c in order}
        json.dump(d, open(PROFILE, 'w'), indent=2)
        print('════ %s: the order %s' % (name, order), flush=True)
        up = restart()
        print('   the engine is back in %.0f s' % up, flush=True)
        m1 = measure(name + '_a', 800)
        m2 = measure(name + '_b', 2050)
        cfg = json.load(open(PROFILE))
        rec = dict(name=name, order=order, slots=None, chunk=None, K=None,
                   read1=m1['reads'], read2=m2['reads'], decode1=m1['decode'],
                   decode2=m2['decode'], wall1=m1['wall'], wall2=m2['wall'],
                   nan=m1['nan'] + m2['nan'])
        with open(LOG, errors='replace') as f:
            f.seek(max(0, os.path.getsize(LOG) - 400000))
            seg = f.read()
        m = re.search(r'expert cache (\d+) slots', seg)
        rec['slots'] = int(m.group(1)) if m else 0
        m = re.search(r'layer split auto: K=([\d,]+)', seg)
        rec['K'] = m.group(1) if m else '?'
        m = re.search(r'prompt chunk (\d+)', seg)
        rec['chunk'] = int(m.group(1)) if m else 0
        with open(OUT, 'a') as f:
            f.write(json.dumps(rec) + '\n')
        print('   %s' % json.dumps({k: rec[k] for k in
              ('read1', 'read2', 'decode1', 'decode2', 'wall1', 'wall2', 'slots', 'chunk', 'K', 'nan')}),
              flush=True)
