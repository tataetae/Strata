#!/usr/bin/env python3
"""The overnight watch: what the engine, the cache and the box are doing, hour by hour.

It watches for the things that would make the owner's work slow or lossy, and it writes
them down so the morning answer is measured rather than remembered:

  a re-prefill     a conversation that was parked and is now read from token 0, or a
                   swap-backed restore (the 5-40 s page-in tax) instead of the RAM-speed one
  an eviction      the engine ERASED a parked conversation (the byte budget or the slot
                   ceiling) - the owner asked for zero evictions, forever
  the NaN fault    #879, the non-finite logits: it wipes the whole cache when it fires
  a queue blow-up  more requests waiting than the engine can plausibly serve - something
                   spawned work that nobody asked for
  the host         MemAvailable, PSI, the swap - the box has to stay usable for the owner

Sampling is read-only.  Nothing here restarts or changes anything; the report says what a
human (or the agent) should do.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.request

ENGINE = os.environ.get("STRATA_ENGINE_URL", "http://127.0.0.1:8080")
QUEUE = os.environ.get("STRATA_QUEUE_VIEW_URL", "http://127.0.0.1:8081/queue")
KEY_FILE = os.environ.get("STRATA_ENGINE_KEY_FILE",
                          os.path.expanduser("~/.config/opencode/strata.key"))
LOG = os.environ.get("STRATA_LOG", "/home/clara/local-inference/strata-src/strata-iq4xs.log")
OUT = os.environ.get("STRATA_WATCH_OUT", "/home/clara/logs/strata-overnight-watch.jsonl")
SUMMARY = os.environ.get("STRATA_WATCH_SUMMARY", "/home/clara/logs/strata-overnight-watch.md")
INTERVAL_S = float(os.environ.get("STRATA_WATCH_INTERVAL_S", "60"))

RESTORE = re.compile(r"conversation cache: restored (\d+) tokens \((\w+)\) in ([\d.]+) ms")
PROMPT = re.compile(r"prompt (\d+) tokens = (\d+) reused \+ (\d+) of (\d+) read in (\d+) ms")
NAN = re.compile(r"non-finite logits \(#879\)|nonfinite")


def _key() -> str:
    try:
        with open(KEY_FILE) as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _json(url: str, key: str = "") -> dict:
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = json.loads(response.read().decode())
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _read(path: str, default: str = "") -> str:
    try:
        with open(path) as handle:
            return handle.read().strip()
    except OSError:
        return default


def _host() -> dict:
    info = {}
    for line in _read("/proc/meminfo").split("\n"):
        if line.startswith(("MemAvailable:", "SwapTotal:", "SwapFree:")):
            key, value = line.split(":", 1)
            info[key] = int(value.split()[0]) // 1024
    try:
        with open("/proc/pressure/memory") as handle:
            for line in handle:
                if line.startswith("some"):
                    info["psi_some_avg10"] = float(line.split("avg10=")[1].split()[0])
    except OSError:
        pass
    try:
        with open("/proc/pressure/cpu") as handle:
            for line in handle:
                if line.startswith("some"):
                    info["psi_cpu_avg10"] = float(line.split("avg10=")[1].split()[0])
    except OSError:
        pass
    return info


def _gpus() -> list:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,utilization.gpu,power.draw,memory.used",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10).stdout.strip().split("\n")
    except Exception:
        return []
    cards = []
    for line in out:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 4:
            cards.append({"index": parts[0], "util": parts[1], "power_w": parts[2],
                          "vram_mib": parts[3]})
    return cards


def _log_events(since: float) -> dict:
    """The engine log's anomalies since a byte offset."""
    counts = {"restores": 0, "slow_restores": 0, "cold_reads": 0, "cold_read_tokens": 0,
              "nan": 0, "parks": 0, "evictions": 0, "slowest_restore_ms": 0.0,
              "slowest_cold_read_ms": 0, "examples": []}
    try:
        size = os.path.getsize(LOG)
    except OSError:
        return counts
    if size < since:
        since = 0
    try:
        with open(LOG, "rb") as handle:
            handle.seek(since)
            blob = handle.read().decode("utf-8", "replace")
    except OSError:
        return counts
    for line in blob.split("\n"):
        if "restored" in line:
            match = RESTORE.search(line)
            if match:
                counts["restores"] += 1
                ms = float(match.group(3))
                counts["slowest_restore_ms"] = max(counts["slowest_restore_ms"], ms)
                if ms > 5000:
                    counts["slow_restores"] += 1
                    if len(counts["examples"]) < 8:
                        counts["examples"].append("slow restore: " + line.strip()[:160])
        elif "strata serve: prompt" in line:
            match = PROMPT.search(line)
            if match:
                tokens, reused = int(match.group(1)), int(match.group(2))
                ms = int(match.group(5))
                if reused == 0 and tokens >= 50000:
                    counts["cold_reads"] += 1
                    counts["cold_read_tokens"] += tokens
                    counts["slowest_cold_read_ms"] = max(counts["slowest_cold_read_ms"], ms)
                    if len(counts["examples"]) < 8:
                        counts["examples"].append("cold read: " + line.strip()[:160])
        if NAN.search(line):
            counts["nan"] += 1
            if len(counts["examples"]) < 8:
                counts["examples"].append("NaN: " + line.strip()[:160])
        if "conversation cache: parked" in line:
            counts["parks"] += 1
            # the park line carries the cache's own eviction counter ("evictions=0")
            match = re.search(r"evictions=(\d+)", line)
            if match and int(match.group(1)) > 0:
                counts["evictions"] += int(match.group(1))
                if len(counts["examples"]) < 8:
                    counts["examples"].append("EVICTED: " + line.strip()[:160])
    return counts


def sample(offset: int) -> tuple[dict, int]:
    key = _key()
    metrics = _json(ENGINE + "/metrics", key)
    queue = _json(QUEUE)
    live = metrics.get("live") or {}
    cache = metrics.get("conversation_cache") or {}
    slots = live.get("slots") or []
    busy = sum(1 for s in slots if (s.get("state") or "idle") != "idle")
    running = int(live.get("running") or 0)
    events = _log_events(offset)
    try:
        offset = os.path.getsize(LOG)
    except OSError:
        pass
    reading = queue.get("reading") or []
    sample_data = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "engine": {
            "state": live.get("state"),
            "running": running,
            "depth": max(0, running - busy),
            "busy_slots": busy,
            "total_slots": live.get("parallel"),
            "prefill_tok_s": live.get("prefill_tok_s_mean"),
        },
        "cache": {
            "parked": cache.get("parked"),
            "slots": cache.get("slots"),
            "budget_mib": cache.get("budget_mib"),
            "mib_used": (cache.get("bytes") or 0) // 1048576,
            "evictions": cache.get("evictions"),
            "requests": cache.get("requests"),
            "requests_reused": cache.get("requests_reused"),
            "reused_tokens": cache.get("reused_tokens"),
            "prompt_tokens": cache.get("prompt_tokens"),
        },
        "queue": {"depth": queue.get("queue_depth") or len(reading),
                  "named": sum(1 for r in reading if r.get("session_id"))},
        "host": _host(),
        "gpus": _gpus(),
        "events": events,
    }
    # the flags: the things that must not happen
    flags = []
    if (cache.get("evictions") or 0) > 0:
        flags.append("the engine ERASED a parked conversation (evictions=%s)" % cache.get("evictions"))
    if events["nan"]:
        flags.append("the NaN fault (#879) fired %d time(s) - it wipes the cache" % events["nan"])
    if sample_data["queue"]["depth"] > 25:
        flags.append("the queue is %d deep - something spawned work" % sample_data["queue"]["depth"])
    mem = sample_data["host"].get("MemAvailable")
    if mem is not None and mem < 2048:
        flags.append("the host has only %d MiB free" % mem)
    psi = sample_data["host"].get("psi_some_avg10")
    if psi is not None and psi > 25:
        flags.append("memory PSI avg10 %.1f" % psi)
    if events["slow_restores"] > 3:
        flags.append("%d swap-backed restores in this sample (the 5-40 s tax)" % events["slow_restores"])
    sample_data["flags"] = flags
    return sample_data, offset


def main() -> int:
    offset = 0
    try:
        offset = os.path.getsize(LOG)
    except OSError:
        pass
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    while True:
        try:
            data, offset = sample(offset)
        except Exception as exc:  # a watcher must not die
            data = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "error": repr(exc)[:200], "flags": []}
        with open(OUT, "a") as handle:
            handle.write(json.dumps(data, default=str) + "\n")
        if data.get("flags"):
            with open(SUMMARY, "a") as handle:
                for flag in data["flags"]:
                    handle.write("- %s  %s\n" % (data["ts"], flag))
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    raise SystemExit(main())
