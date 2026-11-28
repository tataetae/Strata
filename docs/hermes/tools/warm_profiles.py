#!/usr/bin/env python3
"""Warm the engine's conversation cache with each profile's shared prefix.

WHAT THIS IS FOR

Every Hermes profile sends the SAME head with every request: its system prompt (the SOUL.md, the
profile's markdown, the memory) and the tool schemas.  The engine's conversation cache is keyed by
the token prefix, so once that head has been read ONCE, every later session of that profile starts
warm - the engine restores the head's K/V and reads only the conversation's tail.

That state lives in the engine's RAM, so an engine restart empties it and the next session of every
profile pays a cold read of the whole head again.  This script rebuilds it: it replays each
profile's request head (captured verbatim by the clamp proxy, which sees every request Hermes
sends) with a one-token answer, so the engine reads the head and parks it.

The replay carries the EXACT request shape - the same system message, the same tools, the same
reasoning effort - because the engine's chat template renders the head differently for each
(reasoning_effort changes the system text, the tools are rendered into it).

THE CHECKPOINT SETTINGS come with it: the parked entry is built with the engine's own checkpoint
geometry, so its chain is the same one the real turns would have - the restore is compatible.

WHAT IT COSTS: one prefill of the head per profile (10-30 k tokens, 20-60 s each on this box), and
it is worth it only for the profiles whose sessions actually run.  Run it after an engine restart,
or on a timer.

USAGE

  warm_profiles.py                 # warm every profile the proxy has captured
  warm_profiles.py --list          # what is captured, and nothing else
  warm_profiles.py --profile system-maintainer
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

PROXY = os.environ.get("STRATA_QUEUE_VIEW_URL", "http://127.0.0.1:8081")
KEYFILE = os.path.expanduser("~/.config/opencode/strata.key")
ENGINE = os.environ.get("STRATA_ENGINE_URL", "http://127.0.0.1:8080")


def engine_key() -> str:
    try:
        with open(KEYFILE) as fh:
            return fh.read().strip()
    except Exception:
        return ""


def get(url: str, timeout: float = 8.0):
    req = urllib.request.Request(url)
    key = engine_key()
    if key:
        req.add_header("Authorization", "Bearer " + key)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


CAPTURE_DIR = "/home/clara/.hermes/state/prefix_captures"


def captured_prefixes() -> dict:
    """The replayable heads the clamp proxy captured, per profile (it writes them to disk)."""
    out = {}
    try:
        names = sorted(os.listdir(CAPTURE_DIR))
    except OSError:
        return {}
    for name in names:
        if not name.endswith(".json"):
            continue
        profile = name[:-5]
        try:
            with open(os.path.join(CAPTURE_DIR, name)) as fh:
                out[profile] = fh.read()
        except OSError:
            continue
    return out


def warm(profile: str, body: str) -> tuple[bool, str]:
    """Replay one profile's head against the engine."""
    try:
        req = urllib.request.Request(ENGINE + "/v1/chat/completions", data=body.encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        key = engine_key()
        if key:
            req.add_header("Authorization", "Bearer " + key)
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=1800) as resp:
            resp.read()
        return True, "%.0f s" % (time.time() - t0)
    except urllib.error.HTTPError as exc:
        return False, "HTTP %s: %s" % (exc.code, exc.read().decode("utf-8", "replace")[:160])
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    try:
        get(PROXY + "/queue", timeout=6)
    except Exception as exc:
        print("  the clamp proxy is not answering (%s) - is the engine up?" % exc)
        return 1

    heads = captured_prefixes()
    if not heads:
        print("  no profile head has been captured yet - the clamp proxy learns them from the")
        print("  requests Hermes sends; run a turn in a profile first.")
        return 1

    print("  the captured heads:")
    for profile, blob in sorted(heads.items()):
        print("    %-22s %7d bytes" % (profile, len(blob)))
    if args.list:
        return 0

    todo = [args.profile] if args.profile else sorted(heads)
    failed = 0
    for profile in todo:
        blob = heads.get(profile)
        if not blob:
            print("  %-22s no head captured" % profile)
            failed += 1
            continue
        ok, note = warm(profile, blob)
        print("  %-22s %s" % (profile, ("warmed in " + note) if ok else ("FAILED: " + note)))
        if not ok:
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
