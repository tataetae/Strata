#!/usr/bin/env python3
"""Sample the whole inference path while a real Hermes task runs.

Writes one JSON object per line to ~/.hermes/logs/task-YYYYMMDD.jsonl:

  ts            epoch seconds
  engine        live block: state, running, waiting, queued, prompt_read/total,
                generated, tok_s
  cache         expert_cache_mib, expert_slots, ctx, kv, slots, groups
  session       newest non-oneshot session: id, title, msgs, tools, in/out tokens,
                last_activity
  reads         every new prompt-read line the engine wrote since the last sample:
                total, reused, read, ms, rate  (this is the reuse evidence)
  warnings      new warning-looking lines from the engine log

Cheap on purpose: one metrics GET, one sqlite read, one file tail per sample.
"""
import json
import os
import re
import sqlite3
import time
import urllib.request
from pathlib import Path

KEY = Path("/home/clara/.config/opencode/strata.key").read_text().strip()
DB = "/home/clara/.hermes/state.db"
ELOG = "/home/clara/local-inference/strata-src/strata-iq4xs.log"
OUTDIR = Path("/home/clara/.hermes/logs")
SAMPLE_S = 15
READ_RE = re.compile(r"prompt (\d+) tokens = (\d+) reused \+ (\d+) of (\d+) read in (\d+) ms \(([\d.]+) tok/s\)")


def engine():
    try:
        req = urllib.request.Request("http://127.0.0.1:8080/metrics",
                                     headers={"Authorization": f"Bearer {KEY}"})
        with urllib.request.urlopen(req, timeout=5) as r:
            d = json.load(r)
        return d.get("live") or {}, d.get("engine") or {}
    except Exception as exc:  # noqa: BLE001
        return {"state": f"unreachable: {str(exc)[:60]}"}, {}


def sessions(dbs=None, recent_s=3600):
    """Every session with activity in the last hour, across the default home and all profiles.

    Picking only the newest made the log hop between concurrent profiles, so a specific
    chat could not be followed.
    """
    import glob, sqlite3, time
    if dbs is None:
        dbs = [("default", "/home/clara/.hermes/state.db")]
        for path in sorted(glob.glob("/home/clara/.hermes/profiles/*/state.db")):
            dbs.append((path.split("/profiles/")[1].split("/")[0], path))
    cutoff = time.time() - recent_s
    out = []
    for profile, db in dbs:
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                rows = con.execute("""select id, title, message_count, tool_call_count,
                                             input_tokens, output_tokens,
                                             coalesce(last_activity_at, started_at),
                                             last_activity_description
                                      from sessions
                                      where source is not null and source != 'oneshot'
                                        and coalesce(last_activity_at, started_at) > ?
                                      order by coalesce(last_activity_at, started_at) desc""",
                                    (cutoff,)).fetchall()
            finally:
                con.close()
        except Exception:
            continue
        for r in rows:
            out.append({"profile": profile, "id": r[0], "title": (r[1] or "")[:50],
                        "msgs": r[2], "tools": r[3], "in_tokens": r[4], "out_tokens": r[5],
                        "last_at": r[6], "last_activity": (r[7] or "")[:50]})
    out.sort(key=lambda x: x["last_at"] or 0, reverse=True)
    return out


def main():
    OUTDIR.mkdir(parents=True, exist_ok=True)
    pos = {}
    try:
        pos["log"] = os.path.getsize(ELOG)
    except OSError:
        pos["log"] = 0
    while True:
        live, eng = engine()
        out = {"ts": time.time(), "engine": live, "cache": {
            k: eng.get(k) for k in ("expert_cache_mib", "expert_slots", "max_context",
                                    "kv", "batch_slots", "batch_groups", "vram_free_mib")}}
        act = sessions()
        if act:
            out["sessions"] = act
            out["session"] = act[0]      # kept for older readers

        reads, warnings = [], []
        try:
            size = os.path.getsize(ELOG)
            if size > pos["log"]:
                with open(ELOG, "r", errors="replace") as fh:
                    fh.seek(pos["log"])
                    for line in fh:
                        m = READ_RE.search(line)
                        if m:
                            tot, reused, read, tot2, ms, rate = m.groups()
                            reads.append({"total": int(tot), "reused": int(reused),
                                          "read": int(read), "ms": int(ms), "rate": float(rate)})
                        elif "warn" in line.lower() or "fail" in line.lower():
                            warnings.append(line.strip()[:160])
                pos["log"] = size
            elif size < pos["log"]:      # log rotated
                pos["log"] = 0
        except OSError:
            pass
        if reads:
            out["reads"] = reads
        if warnings:
            out["warnings"] = warnings[-5:]

        path = OUTDIR / f"task-{time.strftime('%Y%m%d')}.jsonl"
        with open(path, "a") as fh:
            fh.write(json.dumps(out) + "\n")
        time.sleep(SAMPLE_S)


if __name__ == "__main__":
    main()
