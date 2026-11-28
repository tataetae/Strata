"""The local engine's live state, as a tool: what the model is doing and where this chat stands.

The engine (Strata) publishes its queue, its slots, its cache and its measured rates on
``/metrics``; the clamp proxy in front of it answers ``/queue`` with the same view per chat.
Both are machine-level facts about the box, so this plugin reads them directly rather than
asking the provider for a second opinion.

Read-only: nothing here changes the engine, the queue or a conversation.  It exists so an
agent can say "the engine is busy, you are 3rd of 9, about four minutes" instead of guessing
from a silent wait.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("hermes.plugins.strata_engine_state")

TOOL_NAME = "strata_engine_state"
TOOLSET = "strata-engine"

SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "What the local inference engine is doing right now, and where this chat stands in its queue. "
        "Reports the queue depth, the slots, the prompt being read, the conversation cache's reuse and "
        "an estimate of the wait. Use it when a reply is slow and you need to say WHY (how deep the "
        "queue is, how long a request takes, whether the prompt is being re-read from scratch), or when "
        "the user asks how busy the engine is. Read-only: it never changes the engine or the queue."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "detail": {
                "type": "boolean",
                "description": "True for the full breakdown (per-slot state, cache counters, totals).",
            },
        },
        "required": [],
    },
}


def _engine_url() -> str:
    return os.environ.get("STRATA_ENGINE_URL", "http://127.0.0.1:8080").rstrip("/")


def _queue_url() -> str:
    return os.environ.get("STRATA_QUEUE_VIEW_URL", "http://127.0.0.1:8081/queue")


def _key_file() -> Path:
    default = str(Path.home() / ".config" / "opencode" / "strata.key")
    return Path(os.environ.get("STRATA_ENGINE_KEY_FILE", default))


def _engine_key() -> str:
    """The engine's bearer key: the box's key file. Machine-level, not a profile secret."""
    try:
        return _key_file().read_text().strip()
    except OSError:
        return ""


def _read_json(url: str, key: str = "", timeout: float = 5.0) -> Optional[dict]:
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


def _human_seconds(seconds: float) -> str:
    value = max(0.0, float(seconds))
    if value < 60:
        return "%ds" % int(round(value))
    minutes = value / 60.0
    if minutes < 90:
        return "%d min" % int(round(minutes))
    return "%dh %02d min" % (int(minutes // 60), int(round(minutes % 60)))


def _my_place(view: Optional[dict], session_id: str) -> tuple[Optional[int], Optional[int], Optional[dict]]:
    """(position, depth, row) of this chat in the engine's queue of requests waiting."""
    rows = (view or {}).get("reading") or []
    for index, row in enumerate(rows):
        if session_id and row.get("session_id") == session_id:
            return index + 1, len(rows), row
    return None, len(rows), None


def available() -> bool:
    """True when this box has the local engine configured (its key file is there)."""
    try:
        return _key_file().is_file()
    except OSError:
        return False


def handle(args: Any, session_id: str = "", **_: Any) -> str:
    """The engine's live state, and this chat's place in its queue."""
    detail = bool((args or {}).get("detail"))
    engine_url = _engine_url()
    metrics = _read_json(engine_url + "/metrics", _engine_key())
    if not metrics:
        return (f"The local engine did not answer ({engine_url}/metrics). It may be restarting, "
                f"or its key file may be missing.")

    live = metrics.get("live") or {}
    engine = metrics.get("engine") or {}
    totals = metrics.get("totals") or {}
    cache = metrics.get("conversation_cache") or {}
    slots = live.get("slots") or []
    busy = [s for s in slots if (s.get("state") or "idle") != "idle"]
    running = int(live.get("running") or 0)
    total_slots = int(live.get("parallel") or engine.get("batch_slots") or 1)
    # The requests in flight that do not hold a slot are the ones a new arrival waits
    # behind; the engine's own ``waiting`` counter names only one phase of that queue.
    depth = max(0, running - len(busy))
    requests = int(totals.get("requests") or 0)
    mean_service = None
    if requests > 0:
        spent = float(totals.get("prompt_ms") or 0) + float(totals.get("decode_ms") or 0)
        mean_service = spent / 1000.0 / requests

    lines: list[str] = []
    model = engine.get("model") or "the local model"
    slots_note = "both slots busy" if len(busy) >= total_slots else f"{len(busy)} of {total_slots} slots busy"
    lines.append(f"The engine ({model}): {running} request(s) in flight, {depth} waiting for a slot "
                 f"({slots_note}).")
    reading = [s for s in busy if (s.get("state") or "") == "reading"]
    if reading:
        biggest = max(reading, key=lambda s: s.get("prompt_tokens") or 0)
        tokens = biggest.get("prompt_tokens") or 0
        rate = live.get("prefill_tok_s_mean")
        rate_note = f" at about {rate:.0f} tok/s" if isinstance(rate, (int, float)) and rate else ""
        lines.append(f"It is reading a {tokens:,}-token prompt{rate_note}.")
    else:
        lines.append("It is not reading a prompt right now (the slots are decoding).")

    view = _read_json(_queue_url())
    position, queued, row = _my_place(view, session_id)
    if position:
        lines.append(f"This chat is number {position} of {queued} waiting.")
    elif queued:
        lines.append(f"This chat is not in the waiting list ({queued} other request(s) are).")
    else:
        lines.append("This chat is not waiting on the engine.")

    if mean_service:
        # The requests ahead of this chat: the ones queued before it PLUS the slots that are
        # busy now (they have to finish first).  Without a known place in the queue, the whole
        # depth is ahead.
        ahead = (position - 1 + len(busy)) if position else depth
        estimate = (ahead / max(1, total_slots)) * mean_service
        lines.append(f"A request takes about {_human_seconds(mean_service)} on average, so this chat waits "
                     f"roughly {_human_seconds(estimate)}.")

    reused = cache.get("requests_reused")
    total = cache.get("requests")
    cache_note = f"{cache.get('parked', 0)} parked, {cache.get('evictions', 0)} evicted"
    if total:
        cache_note += f", {reused} of {total} requests reused from the cache"
    lines.append(f"The conversation cache: {cache_note}.")
    if detail:
        lines.append("Slots: " + "; ".join(
            f"slot {s.get('slot')} {s.get('state')} ({s.get('prompt_tokens') or 0} tokens, "
            f"{s.get('tok_s') or 0} tok/s)" for s in slots) + ".")
        lines.append(f"Totals: {requests} requests, {totals.get('prompt_tokens', 0):,} prompt tokens, "
                     f"{totals.get('reused', 0):,} of them reused, "
                     f"{totals.get('output_tokens', 0):,} output tokens.")
    return "\n".join(lines)


def register(ctx) -> None:
    """Plugin entry point, called by the Hermes plugin loader."""
    def handler(args: Any, session_id: str = "", **_: Any) -> str:
        try:
            return handle(args, session_id=session_id)
        except Exception as exc:  # noqa: BLE001 - a tool must not take the turn down
            logger.warning("strata_engine_state: %s", exc, exc_info=True)
            return f"The engine's state could not be read: {exc}"

    tool = {"name": TOOL_NAME, "toolset": TOOLSET, "schema": SCHEMA,
            "handler": handler, "check_fn": available}
    try:
        try:
            ctx.register_tool(**tool, description=SCHEMA["description"], emoji="🛠")
        except TypeError:
            ctx.register_tool(**tool)
    except Exception:  # noqa: BLE001
        logger.warning("strata_engine_state: could not register %s", TOOL_NAME, exc_info=True)
