import json, re, time, unicodedata, urllib.request
KEY = open('/home/clara/.config/opencode/strata.key').read().strip()

def metrics():
    r = urllib.request.Request('http://127.0.0.1:8080/metrics',
                               headers={'Authorization': 'Bearer ' + KEY})
    return json.load(urllib.request.urlopen(r, timeout=20))

def ask(prompt, temp=0.6, n=500):
    body = json.dumps({'model': 'qwen3.8-flash-next-iq4_xs',
                       'messages': [{'role': 'user', 'content': prompt}],
                       'max_tokens': n, 'temperature': temp, 'stream': False,
                       'reasoning_effort': 'none'}).encode()
    req = urllib.request.Request('http://127.0.0.1:8080/v1/chat/completions', data=body,
                                 headers={'Authorization': 'Bearer ' + KEY,
                                          'Content-Type': 'application/json'})
    t0 = time.time()
    d = json.load(urllib.request.urlopen(req, timeout=1200))
    return d['choices'][0]['message'].get('content') or '', time.time() - t0

CJK = re.compile(r'[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]')
THAI = re.compile(r'[\u0e00-\u0e7f]')
LATIN_WORD = re.compile(r'[A-Za-z]{3,}')

PROMPTS = [
    'เขียนบทความสั้น ๆ ภาษาไทยเกี่ยวกับการทำงานที่บ้านและการจัดการเวลา ให้ยาวประมาณ 150 คำ ใช้ภาษาไทยล้วน',
    'อธิบายวิธีดูแลระบบคอมพิวเตอร์สำหรับงาน CAD ในภาษาไทย ยาวประมาณ 120 คำ',
    'เขียนอีเมลถึงลูกค้าเรื่องความล่าช้าของงานจัดส่ง ใช้ภาษาไทยทั้งหมด',
]
tot = {'chars': 0, 'thai': 0, 'cjk': 0, 'latin': 0, 'fffd': 0, 'nfc_bad': 0}
for i, p in enumerate(PROMPTS, 1):
    m0 = metrics().get('totals') or {}
    txt, wall = ask(p)
    m1 = metrics().get('totals') or {}
    off = (m1.get('drafts_offered', 0) - m0.get('drafts_offered', 0))
    acc = (m1.get('drafts_accepted', 0) - m0.get('drafts_accepted', 0))
    nfc = unicodedata.normalize('NFC', txt) == txt
    s = {'chars': len(txt), 'thai': len(THAI.findall(txt)), 'cjk': len(CJK.findall(txt)),
         'latin': len(LATIN_WORD.findall(txt)), 'fffd': txt.count('\ufffd'), 'nfc_ok': nfc}
    for k in ('chars', 'thai', 'cjk', 'latin', 'fffd'):
        tot[k] += s[k]
    if not nfc: tot['nfc_bad'] += 1
    print("  run %d (%5.1fs, drafts %d/%d = %.0f%%): %s" % (
        i, wall, acc, off, 100*acc/off if off else 0, json.dumps(s, ensure_ascii=False)))
    leaked = CJK.findall(txt)[:6]
    if leaked: print("        CJK leak: %s" % ''.join(leaked))
    eng = LATIN_WORD.findall(txt)[:6]
    if eng: print("        latin words: %s" % eng)
print("  TOTAL %s  (the owner's report: 3 CJK + 2 english fragments in 609 chars)" % json.dumps(tot, ensure_ascii=False))
