#!/usr/bin/env python3
"""Cap requested output length between Hermes and the Strata engine.

Hermes derives max_tokens from the remaining context each turn (observed 210k-227k against a
262k window), and there is no single config key that bounds it. This proxy sits on 8081 in
front of the engine on 8080 and rewrites an oversized cap, streaming everything else through
untouched.

    Hermes  ->  :8081 (this)  ->  :8080 (strata)

Set MAX_OUTPUT_TOKENS=0 to disable clamping. Only JSON request bodies are inspected; SSE
responses are relayed byte for byte, so streaming, keepalives and cancellation behave exactly
as before.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from strata_queue_index import SessionIndex
    from strata_queue_registry import RequestRegistry
    INDEX = SessionIndex()
    REGISTRY = RequestRegistry()
except Exception:                                    # the queue view is optional
    INDEX = None
    REGISTRY = None

UPSTREAM = os.environ.get("STRATA_UPSTREAM", "http://127.0.0.1:8080")
LISTEN_PORT = int(os.environ.get("CLAMP_LISTEN_PORT", "8081"))
# A restarting engine REFUSES the socket for 2-4 min while it reloads ~40 GiB of experts. Without a
# retry here, every restart became a 502 to the caller: measured 2026-11-11, 502s clustered exactly on
# the restart minutes (13:47, 13:52) and nowhere else, and the desktop showed "The AI service had a
# problem - custom returned a server error" for a turn that would have succeeded a minute later.
CONNECT_RETRY_SECONDS = float(os.environ.get("STRATA_CONNECT_RETRY_SECONDS", "300") or 0)


def _is_connect_refused(exc: BaseException) -> bool:
    """True only when the socket was never established (engine not listening yet)."""
    if isinstance(exc, ConnectionRefusedError):
        return True
    return isinstance(getattr(exc, "reason", None), ConnectionRefusedError)
CAP = int(os.environ.get("MAX_OUTPUT_TOKENS", "16384") or 0)
_HOP = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "upgrade"}


def clamp_body(raw: bytes) -> tuple[bytes, int | None, int | None]:
    """Return (body, before, after). Only /v1/* JSON bodies are touched."""
    if not CAP or not raw:
        return raw, None, None
    try:
        body = json.loads(raw)
    except Exception:
        return raw, None, None
    if not isinstance(body, dict):
        return raw, None, None
    before = after = None
    keys = ("max_tokens", "max_completion_tokens")
    for key in keys:
        value = body.get(key)
        if isinstance(value, int) and value > CAP:
            before = value if before is None else before
            body[key] = CAP
            after = CAP
    if after is None:
        # Hermes often sends no cap at all, and the engine then sizes one from the remaining
        # context (observed 210k-250k). Inject the ceiling so the turn cannot run for hours.
        if not any(isinstance(body.get(k), int) for k in keys):
            body["max_tokens"] = CAP
            before, after = None, CAP
        else:
            return raw, None, None
    return json.dumps(body).encode(), before, after


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "StrataClamp/1.0"

    def log_message(self, fmt: str, *args) -> None:
        if os.environ.get("CLAMP_VERBOSE"):
            sys.stderr.write("clamp: " + (fmt % args) + "\n")

    @staticmethod
    def _engine_state() -> dict:
        """The engine's own view of the queue: how many requests wait for a slot, what the slots
        hold, and how far each prompt read has got.  The engine publishes it on /metrics; the clamp
        proxy already sits between Hermes and the engine, so it is the natural place to hand it on
        (the dashboard and the phone read this endpoint)."""
        try:
            key = open(os.path.expanduser("~/.config/opencode/strata.key")).read().strip()
            req = urllib.request.Request(UPSTREAM + "/metrics",
                                         headers={"Authorization": "Bearer " + key})
            with urllib.request.urlopen(req, timeout=5) as resp:
                d = json.loads(resp.read().decode())
        except Exception:
            return {}
        live = d.get("live") or {}
        engine = d.get("engine") or {}
        return {
            "state": live.get("state"),
            "running": live.get("running"),
            "waiting": live.get("waiting"),
            "outside_slots": live.get("outside_slots"),
            "slots_total": engine.get("batch_slots"),
            "expert_slots": engine.get("expert_slots"),
            "chunk": engine.get("prefill_cap"),
            "ctx": engine.get("max_context"),
            "kv": engine.get("kv"),
            "slots": [{"slot": s.get("slot"), "state": s.get("state"),
                       "read": s.get("prompt_read"), "total": s.get("prompt_tokens"),
                       "elapsed_s": s.get("elapsed_s")} for s in (live.get("slots") or [])],
        }

    def _queue_view(self) -> None:
        """GET /queue - who is waiting and who is generating, by chat title."""
        view = REGISTRY.view() if REGISTRY else {"reading": [], "generating": []}
        view.setdefault("status", "healthy" if REGISTRY else "unavailable")
        view["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        view["engine"] = self._engine_state()
        body = json.dumps(view, default=str).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _identify(raw: bytes) -> tuple[dict | None, int, str]:
        """(session, prompt_tokens, model) for a chat request; never raises."""
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                return None, 0, ""
            model = body.get("model") if isinstance(body.get("model"), str) else ""
            messages = body.get("messages")
            prompt_tokens = 0
            if isinstance(messages, list):
                # rough: the engine reports the real number; this is only a hint
                prompt_tokens = sum(len(str(m.get("content") or "")) for m in messages)
                prompt_tokens = max(0, prompt_tokens // 4)
            first_user = ""
            if isinstance(messages, list):
                for message in messages:
                    if isinstance(message, dict) and message.get("role") == "user":
                        content = message.get("content")
                        if isinstance(content, str):
                            first_user = content
                        elif isinstance(content, list):
                            first_user = " ".join(
                                str(part.get("text") or "") for part in content
                                if isinstance(part, dict))
                        break
            session = INDEX.lookup(first_user) if (INDEX and first_user) else None
            return session, prompt_tokens, model
        except Exception:
            return None, 0, ""

    def _relay(self) -> None:
        if self.command == "GET" and self.path.rstrip("/") in ("/queue", "/__queue"):
            self._queue_view()
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        session = None
        prompt_tokens = 0
        model = ""
        reg_key = None
        if raw and self.path.startswith(("/v1/chat", "/v1/completions", "/v1/messages", "/v1/responses")):
            raw, before, after = clamp_body(raw)
            if after is not None:
                sys.stderr.write("clamp: max_tokens %s -> %s on %s\n" % (before if before is not None else "absent", after, self.path))
                sys.stderr.flush()
            if REGISTRY is not None:
                session, prompt_tokens, model = self._identify(raw)
                reg_key = REGISTRY.begin(session, prompt_tokens, model)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in _HOP}
        req = urllib.request.Request(UPSTREAM + self.path, data=raw or None,
                                     headers=headers, method=self.command)
        resp = None
        last_error = None
        deadline = time.time() + max(CONNECT_RETRY_SECONDS, 0.0)
        try:
            while True:
                try:
                    resp = urllib.request.urlopen(req, timeout=3600)
                    break
                except urllib.error.HTTPError as exc:  # relay engine errors verbatim
                    resp = exc
                    break
                except Exception as exc:  # upstream down or not listening yet
                    last_error = exc
                    remaining = deadline - time.time()
                    if not _is_connect_refused(exc) or remaining <= 0:
                        break
                    sys.stderr.write("clamp: engine not listening yet, retrying (%.0fs left): %s\n"
                                     % (remaining, exc))
                    sys.stderr.flush()
                    time.sleep(min(2.0, max(0.2, remaining)))
        except Exception as exc:  # unexpected: never leave the caller without a response
            last_error = exc
            resp = None
        if resp is None:
            body = json.dumps({"error": {"message": (
                f"engine not ready after {int(CONNECT_RETRY_SECONDS)}s "
                f"(it reloads its expert cache on start): {last_error}")}}).encode()
            self.send_response(503)
            self.send_header("Retry-After", "30")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            if reg_key:
                REGISTRY.finish(reg_key)
            return

        self.send_response(resp.status)
        for key, value in resp.headers.items():
            if key.lower() not in _HOP:
                self.send_header(key, value)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                if reg_key:
                    REGISTRY.first_byte(reg_key)
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # client hung up; the engine sees its own cancel
        finally:
            if reg_key:
                REGISTRY.finish(reg_key)

    do_GET = _relay
    do_POST = _relay


if __name__ == "__main__":
    if INDEX is not None:
        INDEX.start()
    srv = ThreadingHTTPServer(("127.0.0.1", LISTEN_PORT), Handler)
    sys.stderr.write(f"strata-clamp listening on {LISTEN_PORT} -> {UPSTREAM}, cap={CAP or 'off'}\n")
    if INDEX is not None:
        sys.stderr.write("clamp: queue view on GET /queue (%s)\n" % json.dumps(INDEX.stats()))
    sys.stderr.flush()
    srv.serve_forever()
