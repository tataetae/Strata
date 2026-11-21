#!/usr/bin/env python3
"""Stall watchdog: let the local decision model judge whether the engine is stuck.

Every cycle it gathers a compact state (engine counters, the newest session, and the last
few engine log lines) and asks d1-3B one choice question: is this a stall?  Above the
threshold it writes an alert line and leaves it where the operator will see it.

Two deliberate limits: it never restarts or cancels anything, and it never sends a message
outside this machine.

Usage: stall-watch.py [--once] [--minutes N]
"""
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

KEY = Path("/home/clara/.config/opencode/strata.key").read_text().strip()
D1 = "http://127.0.0.1:8790/v1/systemone"
ELOG = "/home/clara/local-inference/strata-src/strata-iq4xs.log"
ALERTS = Path("/home/clara/.hermes/logs/stall-alerts.log")
STATE = Path("/home/clara/.hermes/logs/stall-watch-state.json")
THRESHOLD = 0.70          # below this, d1 is not sure enough to raise an alert
STILL_MINUTES = 6.0       # how long "no progress" must last before we even ask

DBS = [("default", "/home/clara/.hermes/state.db")]
for p in sorted(Path("/home/clara/.hermes/profiles").glob("*/state.db")):
    DBS.append((p.parent.name, str(p)))


def engine():
    try:
        req = urllib.request.Request("http://127.0.0.1:8080/metrics",
                                     headers={"Authorization": f"Bearer {KEY}"})
        with urllib.request.urlopen(req, timeout=6) as r:
            d = json.load(r)
        return d.get("live") or {}
    except Exception as exc:  # noqa: BLE001
        return {"state": f"unreachable: {str(exc)[:50]}"}


def newest_session():
    import sqlite3
    best = None
    for profile, db in DBS:
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                row = con.execute("""select id,title,message_count,input_tokens,output_tokens,
                                            coalesce(last_activity_at,started_at),last_activity_description
                                     from sessions where source is not null and source!='oneshot'
                                     order by coalesce(last_activity_at,started_at) desc limit 1""").fetchone()
            finally:
                con.close()
        except Exception:
            continue
        if row and (best is None or (row[5] or 0) > best[2]):
            best = (profile, row, row[5] or 0)
    if not best:
        return None
    profile, row, _ = best
    return {"profile": profile, "id": row[0], "title": (row[1] or "")[:40], "msgs": row[2],
            "in_tokens": row[3], "out_tokens": row[4], "last_at": row[5],
            "activity": (row[6] or "")[:60]}


def log_tail(n=6):
    try:
        lines = open(ELOG, errors="replace").read().splitlines()[-n:]
        return "\n".join(l[:160] for l in lines)
    except OSError:
        return ""


def ask_d1(state_text, question, options):
    body = json.dumps({
        "state": state_text,
        "questions": {"q": {"instructions": question, "type": "choice",
                            "criteria": {o: None for o in options}}},
    }).encode()
    req = urllib.request.Request(D1, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        d = json.load(r)
    ans = d.get("answers") or d.get("results") or {}
    q = ans.get("q") or {}
    return q.get("probabilities") or {}


def load_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def save_state(s):
    STATE.write_text(json.dumps(s))


def alert(msg):
    ALERTS.parent.mkdir(parents=True, exist_ok=True)
    with open(ALERTS, "a") as fh:
        fh.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    print("  ALERT:", msg)


def cycle(still_minutes=STILL_MINUTES):
    live = engine()
    sess = newest_session()
    st = load_state()

    slots = live.get("parallel") or len(live.get("slots") or []) or 1
    running = live.get("running") or 0
    read = live.get("prompt_read") or 0

    key = (sess or {}).get("id")
    progressed = (sess or {}).get("msgs")
    prev = st.get(key, {})
    if prev.get("msgs") != progressed or prev.get("read") != read:
        st[key] = {"msgs": progressed, "read": read, "since": time.time()}
        save_state(st)
        print("  %s progress: msgs=%s read=%s" % (time.strftime("%H:%M:%S"), progressed, read))
        return None

    still_s = time.time() - (prev.get("since") or time.time())
    print("  %s no progress for %.1f min (msgs=%s, state=%s, running=%s/%s, read=%s/%s)" % (
        time.strftime("%H:%M:%S"), still_s / 60, progressed, live.get("state"),
        running, slots, read, live.get("prompt_total")))

    if still_s < still_minutes * 60:
        return None

    state_text = (
        "Engine state: %s\n"
        "Slots busy: %s of %s, in flight: %s\n"
        "Prompt read: %s of %s tokens\n"
        "Session: %r (%s), messages %s, no change for %.1f minutes\n"
        "Recent engine log:\n%s"
        % (live.get("state"), live.get("slots") and
           sum(1 for s in live["slots"] if (s.get("state") or "idle") != "idle"),
           slots, running, read, live.get("prompt_total"),
           (sess or {}).get("title"), (sess or {}).get("profile"), progressed,
           still_s / 60, log_tail())
    )
    try:
        probs = ask_d1(state_text, "Is this inference server stalled and in need of attention?",
                       ["yes", "no"])
    except Exception as exc:  # noqa: BLE001
        print("  d1 unavailable:", str(exc)[:60])
        return None
    p_yes = probs.get("yes", 0.0)
    print("     d1: stall yes=%.3f no=%.3f" % (p_yes, probs.get("no", 0.0)))
    if p_yes >= THRESHOLD:
        alert("STALL %.2f  %s[%s] msgs=%s still=%.0f min state=%s read=%s/%s" % (
            p_yes, (sess or {}).get("title"), (sess or {}).get("profile"), progressed,
            still_s / 60, live.get("state"), read, live.get("prompt_total")))
        return p_yes
    return None


def main():
    once = "--once" in sys.argv
    minutes = STILL_MINUTES
    if "--minutes" in sys.argv:
        minutes = float(sys.argv[sys.argv.index("--minutes") + 1])
    if once:
        cycle(still_minutes=minutes)
        return 0
    while True:
        try:
            cycle(still_minutes=minutes)
        except Exception as exc:  # noqa: BLE001
            print("  cycle error:", str(exc)[:80])
        time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
