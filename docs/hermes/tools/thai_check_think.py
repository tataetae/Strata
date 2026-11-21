import json, re, time, unicodedata, urllib.request
KEY = open('/home/clara/.config/opencode/strata.key').read().strip()

def ask(prompt, n=3000, effort=None):
    body = {'model': 'qwen3.8-flash-next-iq4_xs',
            'messages': [{'role': 'user', 'content': prompt}],
            'max_tokens': n, 'temperature': 0.6, 'stream': False}
    if effort: body['reasoning_effort'] = effort
    req = urllib.request.Request('http://127.0.0.1:8080/v1/chat/completions',
                                 data=json.dumps(body).encode(),
                                 headers={'Authorization': 'Bearer ' + KEY,
                                          'Content-Type': 'application/json'})
    t0 = time.time()
    d = json.load(urllib.request.urlopen(req, timeout=2400))
    m = d['choices'][0]['message']
    return (m.get('content') or ''), (m.get('reasoning_content') or ''), time.time() - t0, d.get('usage')

CJK  = re.compile(r'[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]')
THAI = re.compile(r'[\u0e00-\u0e7f]')
LAT  = re.compile(r'[A-Za-z]{3,}')

PROMPTS = [
    'เขียนบทความสั้น ๆ ภาษาไทยเกี่ยวกับการทำงานที่บ้านและการจัดการเวลา ให้ยาวประมาณ 150 คำ ใช้ภาษาไทยล้วน',
    'อธิบายวิธีดูแลระบบคอมพิวเตอร์สำหรับงาน CAD ในภาษาไทย ยาวประมาณ 120 คำ',
]
for i, p in enumerate(PROMPTS, 1):
    ans, think, wall, usage = ask(p)
    print("  run %d: wall %.1fs  chars(ans)=%d  chars(think)=%d  usage=%s" % (i, wall, len(ans), len(think), json.dumps(usage)))
    for label, txt in (('answer', ans), ('thinking', think)):
        if not txt: continue
        cjk = CJK.findall(txt); lat = LAT.findall(txt)
        print("    %-8s thai=%4d cjk=%d %s | latin=%d %s | fffd=%d | nfc=%s" % (
            label, len(THAI.findall(txt)), len(cjk), (''.join(cjk[:8]) if cjk else ''),
            len(lat), (lat[:8] if lat else ''), txt.count('\ufffd'),
            unicodedata.normalize('NFC', txt) == txt))
    if ans: print("    answer head: %s" % ans[:160].replace('\n', ' '))
