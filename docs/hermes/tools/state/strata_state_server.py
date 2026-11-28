#!/usr/bin/env python3
"""The engine's live state, on the LAN.

The queue view (the clamp proxy's ``/queue``) and the engine's own ``/metrics`` are both
loopback-only: the proxy because it sits in the request path, the metrics because they name
the chats.  That makes them unreachable from the owner's phone or another machine without
the VPN-and-nginx path, which is not reliable.

This is a small read-only front for both, on the LAN, behind the engine's own bearer key:

    GET /          a tiny page: the queue, the engine's state, the cache (a browser bookmark)
    GET /queue     the same JSON the clamp proxy serves (the chats, their places, the engine)
    GET /state     the engine's own numbers: the queue depth, the slots, the cache, the rates
    GET /health    public: is this service alive

Nothing here changes the engine, the queue or a conversation.  The key is the engine's own
(one of the comma-separated list in ``strata.env``); the LAN key is in
``~/.config/opencode/strata.lan.key``.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

PORT = int(os.environ.get("STRATA_STATE_PORT", "9000"))
ENGINE_URL = os.environ.get("STRATA_ENGINE_URL", "http://127.0.0.1:8080").rstrip("/")
QUEUE_URL = os.environ.get("STRATA_QUEUE_VIEW_URL", "http://127.0.0.1:8081/queue")
KEY_FILE = os.environ.get("STRATA_ENGINE_KEY_FILE",
                          str(Path.home() / ".config" / "opencode" / "strata.key"))
LAN_KEY_FILE = os.environ.get("STRATA_LAN_KEY_FILE",
                               str(Path.home() / ".config" / "opencode" / "strata.lan.key"))

_METRICS_TTL_S = 3.0
_lock = threading.Lock()
_metrics_cache: tuple[float, Optional[dict]] = (0.0, None)


def _read(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def _keys() -> list[str]:
    """Every key the engine accepts (its key list may hold several)."""
    found = []
    for path in (KEY_FILE, LAN_KEY_FILE):
        value = _read(path)
        if value:
            found.append(value)
    return found


def _fetch(url: str, key: str = "", timeout: float = 5.0) -> Optional[dict]:
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode())
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _metrics() -> Optional[dict]:
    """The engine's own /metrics, briefly cached (the engine's numbers move every second)."""
    global _metrics_cache
    now = time.monotonic()
    with _lock:
        stamp, cached = _metrics_cache
        if cached is not None and now - stamp < _METRICS_TTL_S:
            return cached
    payload = None
    for key in _keys():
        payload = _fetch(ENGINE_URL + "/metrics", key)
        if payload:
            break
    with _lock:
        _metrics_cache = (time.monotonic(), payload)
    return payload


def _summary(metrics: Optional[dict]) -> dict:
    """The engine's numbers, reduced to what a page (or a phone) needs."""
    if not metrics:
        return {"error": "the engine did not answer %s/metrics" % ENGINE_URL}
    live = metrics.get("live") or {}
    engine = metrics.get("engine") or {}
    totals = metrics.get("totals") or {}
    cache = metrics.get("conversation_cache") or {}
    slots = live.get("slots") or []
    busy = [s for s in slots if (s.get("state") or "idle") != "idle"]
    running = int(live.get("running") or 0)
    requests = int(totals.get("requests") or 0)
    mean_service = None
    if requests > 0:
        spent = float(totals.get("prompt_ms") or 0) + float(totals.get("decode_ms") or 0)
        mean_service = spent / 1000.0 / requests
    return {
        "model": engine.get("model"),
        "state": live.get("state"),
        "phase": live.get("phase"),
        "running": running,
        "waiting_for_a_slot": max(0, running - len(busy)),
        "busy_slots": len(busy),
        "total_slots": int(live.get("parallel") or engine.get("batch_slots") or 1),
        "prefill_tok_s": live.get("prefill_tok_s_mean"),
        "prompt_read": live.get("prompt_read"),
        "prompt_total": live.get("prompt_total"),
        "expert_slots": engine.get("expert_slots"),
        "context": engine.get("max_context"),
        "kv": engine.get("kv"),
        "mean_service_s": mean_service,
        "requests": requests,
        "prompt_tokens": totals.get("prompt_tokens"),
        "reused_tokens": totals.get("reused"),
        "output_tokens": totals.get("output_tokens"),
        "cache": {
            "parked": cache.get("parked"),
            "bytes": cache.get("bytes"),
            "evictions": cache.get("evictions"),
            "requests": cache.get("requests"),
            "requests_reused": cache.get("requests_reused"),
        },
        "cache_slots": cache.get("slots"),
        "slots": [{"slot": s.get("slot"), "state": s.get("state"),
                   "prompt_tokens": s.get("prompt_tokens"), "generated": s.get("generated"),
                   "elapsed_s": s.get("elapsed_s"), "tok_s": s.get("tok_s")} for s in slots],
    }


def _queue() -> dict:
    """The clamp proxy's queue view: the chats, their places, and the engine's block.

    The rows arrive oldest-first, so a row's position IS its place in the queue.  The proxy
    labels the rows itself once it is restarted with that change; until then the position is
    filled in here, so the LAN view is right either way.
    """
    view = _fetch(QUEUE_URL) or {}
    view.setdefault("status", "unavailable")
    reading = view.get("reading") or []
    for index, item in enumerate(reading):
        if not isinstance(item, dict):
            continue
        if not isinstance(item.get("position"), int):
            item["position"] = index + 1
    if not isinstance(view.get("queue_depth"), int):
        view["queue_depth"] = len(reading)
    return view


PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>The engine's state</title>
<style>
 body{font:14px/1.45 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;margin:0;padding:16px;background:#111;color:#ddd}
 h1{font-size:17px;margin:0 0 4px} .sub{color:#8a8a8a;margin-bottom:14px;font-size:12px}
 input,button{font:inherit;padding:7px 10px;border-radius:8px;border:1px solid #333;background:#1a1a1a;color:#ddd}
 input{width:210px} button{cursor:pointer;background:#262626}
 .cards{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}
 .card{background:#1a1a1a;border:1px solid #262626;border-radius:10px;padding:8px 12px;min-width:96px}
 .card b{display:block;font-size:19px} .card span{color:#8a8a8a;font-size:11px;text-transform:uppercase}
 table{width:100%;border-collapse:collapse;margin-top:8px}
 th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #242424;font-size:13px}
 th{color:#8a8a8a;font-weight:500;font-size:11px;text-transform:uppercase}
 .tag{color:#7fd1a0} .pos{color:#e0b060;font-weight:600}
 .err{color:#e07f7f}
</style>
<h1>The engine's state</h1>
<div class="sub">clara-ai-server &middot; Strata &middot; the queue, the slots, the cache</div>
<input id="k" type="password" placeholder="the engine key">
<button onclick="load()">refresh</button>
<div id="out" class="cards"></div>
<div id="cache"></div>
<div id="rows"></div>
<script>
let key = localStorage.getItem('strataKey') || '';
document.getElementById('k').value = key;
async function load(){
  const k = document.getElementById('k').value.trim();
  localStorage.setItem('strataKey', k);
  const h = {Authorization: 'Bearer ' + k};
  const [state, queue] = await Promise.all([
    fetch('/state', {headers:h}).then(r=>r.json()),
    fetch('/queue', {headers:h}).then(r=>r.json())]);
  const s = state.error ? {error: state.error} : state;
  const cache = s.cache || {};
  const cards = s.error ? '' : [
    ['in flight', s.running], ['waiting for a slot', s.waiting_for_a_slot],
    ['batch slots', s.busy_slots + '/' + s.total_slots], ['read tok/s', s.prefill_tok_s || '-'],
    ['parked chats', cache.parked || 0], ['evictions', cache.evictions || 0]].map(c =>
      '<div class="card"><b>'+c[1]+'</b><span>'+c[0]+'</span></div>').join('');
  document.getElementById('out').innerHTML = s.error ? '<div class="err">'+s.error+'</div>' : cards;
  const cacheNote = s.error ? '' :
    '<div class="sub">the conversation cache: '+ (cache.parked || 0) +' of ' + (s.cache_slots || '?') +
    ' conversations parked' + (cache.bytes ? ' · ' + (cache.bytes/1073741824).toFixed(1) + ' GiB' : '') +
    ' · the engine has ' + (s.total_slots||'?') + ' batch slots (concurrent requests)</div>';
  document.getElementById('cache').innerHTML = cacheNote;
  const rows = (queue.reading||[]).concat(queue.generating||[]);
  document.getElementById('rows').innerHTML = rows.length ? '<table><tr><th>#</th><th>chat</th>'+
    '<th>phase</th><th>waiting</th><th>tokens</th></tr>' + rows.map(r =>
      '<tr><td class="pos">'+(r.position||'')+'</td><td>'+(r.profile?r.profile+' · ':'')+
      (r.title||'')+'</td><td class="tag">'+(r.tag||'')+'</td><td>'+Math.round(r.for_s||0)+'s</td><td>'+
      (r.prompt_tokens||'')+'</td></tr>').join('') + '</table>' : '<div class="sub">nothing in flight</div>';
}
load(); setInterval(load, 5000);
</script>
"""


def _listeners() -> list[tuple[str, int]]:
    """(host, port) pairs to serve on.

    Two, on purpose:

      - the requested port on every interface (0.0.0.0), which needs a firewall line;
      - the LAN address on the health API's port, which the firewall ALREADY allows.  The
        health API keeps 127.0.0.1:9120 for itself: a specific address and the wildcard are
        different sockets, so both can listen on 9120 at the same time.

    The LAN address is discovered (the source address of the default route), so the unit
    does not carry a stale IP.
    """
    pairs = [(os.environ.get("STRATA_STATE_HOST", "0.0.0.0"), PORT)]
    lan_port = int(os.environ.get("STRATA_STATE_LAN_PORT", "9120"))
    lan_host = os.environ.get("STRATA_STATE_LAN_HOST", "")
    if not lan_host:
        lan_host = _primary_address()
    if lan_host and lan_port:
        pairs.append((lan_host, lan_port))
    return pairs


def _primary_address() -> str:
    """The box's own address on its default route (the LAN side)."""
    try:
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 80))  # TEST-NET-1: routed, never answered
            return probe.getsockname()[0]
    except Exception:
        return ""


class Handler(BaseHTTPRequestHandler):
    server_version = "strata-state/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: Any) -> None:
        self._send(code, json.dumps(payload, default=str).encode(), "application/json")

    def _authorised(self) -> bool:
        header = self.headers.get("Authorization") or ""
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not token:
            # a bookmark on a phone cannot set a header: allow the key as a query parameter.
            token = (self.path.split("?", 1)[1] if "?" in self.path else "")
            for part in token.split("&"):
                if part.startswith("key="):
                    token = part[4:]
                    break
            else:
                token = ""
        return any(token and token == candidate for candidate in _keys())

    def do_GET(self) -> None:  # noqa: N802 - the http.server contract
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/health":
            self._json(200, {"status": "ok", "engine_url": ENGINE_URL,
                             "engine": "up" if _metrics() else "down"})
            return
        if path == "/":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            return
        if not self._authorised():
            self._json(401, {"error": "the engine's bearer key is required"})
            return
        if path == "/queue":
            self._json(200, _queue())
            return
        if path == "/state":
            self._json(200, _summary(_metrics()))
            return
        self._json(404, {"error": "not found"})


def main() -> int:
    servers = []
    for host, port in _listeners():
        try:
            servers.append((host, port, ThreadingHTTPServer((host, port), Handler)))
        except OSError as exc:
            sys.stderr.write("strata-state: cannot bind %s:%d (%s)\n" % (host, port, exc))
    if not servers:
        return 1
    for host, port, server in servers:
        sys.stderr.write("strata-state listening on %s:%d -> %s\n" % (host, port, ENGINE_URL))
    sys.stderr.flush()
    for _, _, server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    for _, _, server in servers:
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
