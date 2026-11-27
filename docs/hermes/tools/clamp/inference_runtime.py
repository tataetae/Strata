"""Read the live Strata endpoint for the Clara dashboard.

The old dashboard probed a Docker llama.cpp container. The serving stack is now
native Strata on 127.0.0.1:8080, so this module speaks HTTP only. No credential
is stored here; the local Strata server currently requires no API key.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any
from urllib.request import Request, urlopen

CONTAINER_NAME = os.environ.get("HERMES_INFERENCE_CONTAINER", "strata-iq3s")
BASE_URL = os.environ.get("STRATA_BASE_URL", "http://127.0.0.1:8080").rstrip("/")


def _key_from_file() -> str:
    """The engine requires a bearer key; without it /slots and /metrics answer 401 and the
    dashboard's live panel silently reports an idle engine.  The key lives in the same file
    the rest of the stack reads."""
    try:
        with open(os.path.join(os.path.expanduser("~"), ".config", "opencode", "strata.key")) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _first_key(value: str) -> str:
    """The engine accepts a COMMA-separated list of keys (serve/server.py::api_key_of), but a
    bearer token is ONE value: sending the whole list is a 401.  The env/file may hold the
    list (a second key for a LAN client), so a client takes the first entry."""
    for part in str(value or "").split(","):
        part = part.strip()
        if part:
            return part
    return ""


API_KEY = _first_key(os.environ.get("STRATA_API_KEY") or _key_from_file())
CONFIG_TTL_SECONDS = 5.0
_lock = threading.Lock()
_cached: dict[str, Any] | None = None
_cached_at = 0.0


def _get(path: str, timeout: float = 3.0) -> Any:
    headers = {"User-Agent": "HermesHealthApi/1.0"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    request = Request(BASE_URL + path, headers=headers)
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _read_runtime_config() -> dict[str, Any]:
    try:
        health = _get("/health")
        if not health.get("loaded"):
            return {"status": "unavailable", "error": "Strata model is not loaded"}
        return {
            "status": "healthy",
            "container": CONTAINER_NAME,
            "base_url": BASE_URL,
            "api_key": API_KEY or None,
            "model": health.get("model"),
            "args": [],
            "native_quantized_attention_requested": False,
            "pinned_host_buffers_requested": True,
        }
    except Exception:  # noqa: BLE001 - the dashboard must degrade, never raise
        return {"status": "unavailable", "error": "Strata server is not reachable"}


def get_runtime_config(force_refresh: bool = False) -> dict[str, Any]:
    global _cached, _cached_at
    now = time.monotonic()
    with _lock:
        if not force_refresh and isinstance(_cached, dict) \
                and _cached.get("status") == "healthy" \
                and now - _cached_at < CONFIG_TTL_SECONDS:
            return dict(_cached)
        result = _read_runtime_config()
        _cached, _cached_at = result, now
        return dict(result)


def authorization_headers(config: dict[str, Any] | None = None) -> dict[str, str]:
    key = (config or {}).get("api_key") or API_KEY
    return {"Authorization": f"Bearer {key}"} if key else {}


def safe_server_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    runtime = config or get_runtime_config()
    if runtime.get("status") != "healthy":
        return {"status": "unavailable", "error": runtime.get("error")}
    try:
        props = _get("/props")
        metrics = _get("/metrics")
    except Exception:  # noqa: BLE001
        return {"status": "unavailable", "error": "Strata properties are not reachable"}
    generation = props.get("default_generation_settings") or {}
    params = generation.get("params") or {}
    engine = metrics.get("engine") or {}
    return {
        "status": "healthy",
        "model": runtime.get("model") or props.get("model_alias"),
        "context_size": generation.get("n_ctx"),
        "context_per_slot": generation.get("n_ctx"),
        "parallel": props.get("total_slots"),
        "kv": engine.get("kv"),
        "kv_resident": engine.get("kv_resident"),
        "batch_slots": engine.get("batch_slots"),
        "batch_groups": engine.get("batch_groups"),
        "expert_cache_mib": engine.get("expert_cache_mib"),
        "expert_slots": engine.get("expert_slots"),
        "vram_free_mib": engine.get("vram_free_mib"),
        "arena_mib": engine.get("arena_mib"),
        "spec": engine.get("spec"),
        "spec_min_p": engine.get("spec_min_p"),
        "drafting": engine.get("drafting"),
        "mtp_max": engine.get("mtp_max"),
        "sampling": params,
        "gpu": [0, 1, 2, 3],
        "gpu_roles": "GPU0=main; helpers 1,2,3",
        "container_name": CONTAINER_NAME,
        "engine_version": engine.get("version") or (metrics.get("engine") or {}).get("version"),
        "backend": "strata",
    }
