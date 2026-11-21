"""Read-only health API for the WSL-native Hermes stack.

The service binds to Windows loopback. The Ubuntu relay consumes it through the
existing reverse SSH tunnel, so no health listener is exposed to the LAN.
"""

from __future__ import annotations

import collections
import ctypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
from pathlib import Path
import socket
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import psutil

from inference_runtime import (
    CONTAINER_NAME as LLAMA_CONTAINER, authorization_headers, get_runtime_config, safe_server_config,
)
try:
    from inference_capture_store import recent_capture_snapshot
except ImportError:
    recent_capture_snapshot = None


HOST = "127.0.0.1"
PORT = 9120
SAMPLE_INTERVAL_SECONDS = 60
GPU_SAMPLE_INTERVAL_SECONDS = 1   # owner request 2026-11-01: 1 s samples; nvidia-smi costs ~40 ms/CPU per call
GPU_STALE_AFTER_SECONDS = 6
# Use the native WSL driver tool: no Windows process launch or per-client query.
# WSL used to ship its own binary at /usr/lib/wsl/lib/nvidia-smi; on native Ubuntu use the
# system one. The old hardcoded path made every GPU sample fail ("query failed or timed out").
LIVE_NVIDIA_SMI = shutil.which("nvidia-smi") or "/usr/bin/nvidia-smi"
HOST_NVIDIA_SMI = Path("/mnt/c/Windows/System32/nvidia-smi.exe")
NVIDIA_SMI = str(HOST_NVIDIA_SMI) if HOST_NVIDIA_SMI.exists() else "nvidia-smi"
HTTP_TIMEOUT_SECONDS = 3
HOST_COMMIT_SNAPSHOT = Path(
    os.environ.get(
        "HERMES_WINDOWS_COMMIT_SNAPSHOT",
        "/mnt/d/Hermes/runtime/watchdogs/windows-commit.json",
    )
)
HOST_COMMIT_SNAPSHOT_MAX_AGE_SECONDS = 180

HERMES_ROOT = Path(
    os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
).expanduser()
LOG_ROOT = HERMES_ROOT / "logs"
LOG_ROOT.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("hermes-health-api")
logger.setLevel(logging.INFO)
handler = RotatingFileHandler(
    LOG_ROOT / "health-api.log", maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8"
)
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(handler)


SERVICES: dict[str, dict[str, Any]] = {
    "hermes_gateway": {
        "url": "http://127.0.0.1:8647/v1/models",
        "expected": {200, 401},
        "critical": True,
    },
    "hermes_dashboard": {
        "url": "http://127.0.0.1:9119/",
        "expected": {200, 302},
        "critical": False,
    },
    "hindsight": {
        "url": "http://127.0.0.1:8888/health",
        "expected": {200},
        "critical": True,
    },
    "strata": {
        "url": "http://127.0.0.1:8080/health",
        "expected": {200},
        "critical": True,
    },
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat().replace("+00:00", "Z")


def windows_commit_details() -> dict[str, Any]:
    """Return the system commit limit and headroom.

    psutil.swap_memory() reports page-file occupancy, which is not the same as
    Windows' commit charge. CUDA/WDDM reservations can exhaust the commit limit
    while the page file is nearly empty, so both measurements are required.
    """

    limit = 0
    available = 0
    source = "psutil_fallback"
    snapshot_age_seconds: float | None = None
    if os.name == "nt":
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            limit = int(status.ullTotalPageFile)
            available = int(status.ullAvailPageFile)
            source = "windows_global_memory_status"
    else:
        # Never launch PowerShell/CIM from the WSL sampling loop. If Windows
        # interop or WMI stalls, terminating the Linux-side wrapper does not
        # reliably terminate the Windows child. Repeated samples can then leak
        # hundreds of powershell.exe processes and eventually block both WSL
        # and Windows process creation. A single-instance Windows task writes
        # this snapshot using GlobalMemoryStatusEx instead.
        try:
            payload = json.loads(HOST_COMMIT_SNAPSHOT.read_text(encoding="utf-8-sig"))
            snapshot_age_seconds = max(0.0, time.time() - float(payload["sampled_unix"]))
            if snapshot_age_seconds <= HOST_COMMIT_SNAPSHOT_MAX_AGE_SECONDS:
                limit = int(payload["limit_bytes"])
                available = int(payload["available_bytes"])
                source = "windows_host_snapshot"
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            pass
    if not limit:
        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
        limit = int(memory.total + swap.total)
        available = int(memory.available + swap.free)
    used = max(0, limit - available)
    if available < 2 * 1024**3:
        health = "unhealthy"
    elif available < 8 * 1024**3:
        health = "degraded"
    else:
        health = "healthy"
    return {
        "status": health,
        "limit_bytes": limit,
        "used_bytes": used,
        "available_bytes": available,
        "used_percent": round(used / limit * 100, 1) if limit else None,
        "source": source,
        "snapshot_age_seconds": (
            round(snapshot_age_seconds, 1) if snapshot_age_seconds is not None else None
        ),
    }


def request_status(url: str) -> tuple[int, float, str | None]:
    started = time.monotonic()
    try:
        request = Request(url, headers={"User-Agent": "HermesHealthApi/1.0"})
        with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            status = int(response.status)
        return status, round((time.monotonic() - started) * 1000, 1), None
    except HTTPError as exc:
        return int(exc.code), round((time.monotonic() - started) * 1000, 1), None
    except (URLError, TimeoutError, OSError) as exc:
        return 0, round((time.monotonic() - started) * 1000, 1), str(exc)


def read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def process_details(pid: int) -> dict[str, Any]:
    if pid <= 0:
        return {"pid": pid, "running": False}
    try:
        process = psutil.Process(pid)
        memory = process.memory_info()
        return {
            "pid": pid,
            "running": process.is_running(),
            "name": process.name(),
            "memory_rss_bytes": memory.rss,
            "memory_private_bytes": getattr(memory, "private", None),
            "handles": process.num_handles() if hasattr(process, "num_handles") else None,
            "started_at": iso_utc(datetime.fromtimestamp(process.create_time(), timezone.utc)),
        }
    except (psutil.Error, OSError):
        return {"pid": pid, "running": False}


def heartbeat_details(path: Path) -> dict[str, Any]:
    heartbeat = read_json(path)
    if not isinstance(heartbeat, dict):
        return {"present": False, "fresh": False}
    result: dict[str, Any] = {"present": True}
    try:
        updated = datetime.fromisoformat(str(heartbeat["updated_at"]).replace("Z", "+00:00"))
        age = max(0.0, (utc_now() - updated.astimezone(timezone.utc)).total_seconds())
        result.update(
            {
                "updated_at": iso_utc(updated.astimezone(timezone.utc)),
                "age_seconds": round(age, 1),
                "fresh": age <= 150,
            }
        )
    except (KeyError, TypeError, ValueError):
        result.update({"fresh": False, "error": "invalid updated_at"})
    pid = int(heartbeat.get("pid") or 0)
    result["process"] = process_details(pid)
    return result


def tunnel_details() -> dict[str, Any]:
    expected_forward = "127.0.0.1:19120:127.0.0.1:9120"
    try:
        for process in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
            name = (process.info.get("name") or "").lower()
            if name not in {"ssh", "ssh.exe"}:
                continue
            command = " ".join(process.info.get("cmdline") or [])
            if "deploy@192.168.1.80" in command and expected_forward in command:
                return {
                    "status": "healthy",
                    "pid": process.info["pid"],
                    "server": "192.168.1.80",
                    "remote_url": "http://127.0.0.1:19120/v1/health",
                    "started_at": iso_utc(
                        datetime.fromtimestamp(float(process.info["create_time"]), timezone.utc)
                    ),
                }
    except (psutil.Error, OSError, TypeError, ValueError):
        pass
    return {
        "status": "unhealthy",
        "server": "192.168.1.80",
        "remote_url": "http://127.0.0.1:19120/v1/health",
    }


def ollama_models() -> list[dict[str, Any]]:
    try:
        request = Request("http://127.0.0.1:11434/api/ps", headers={"User-Agent": "HermesHealthApi/1.0"})
        with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            payload = json.load(response)
        models = []
        for model in payload.get("models", []):
            models.append(
                {
                    "name": model.get("name") or model.get("model"),
                    "size_bytes": model.get("size"),
                    "size_vram_bytes": model.get("size_vram"),
                    "expires_at": model.get("expires_at"),
                }
            )
        return models
    except (OSError, ValueError, TypeError, URLError):
        return []


def _number(value: str) -> float | None:
    try:
        cleaned = value.strip().replace("[N/A]", "").replace("N/A", "")
        return float(cleaned) if cleaned else None
    except (TypeError, ValueError):
        return None


def nvidia_gpu_details() -> dict[str, Any]:
    """Return physical VRAM and load for every NVIDIA GPU.

    llama.cpp on Windows/WDDM does not report per-process VRAM through
    --query-compute-apps, so device totals are the authoritative source.
    """
    query = (
        "index,uuid,name,memory.total,memory.used,memory.free,"
        "utilization.gpu,temperature.gpu,power.draw,"
        "clocks_event_reasons.sw_thermal_slowdown"
    )
    try:
        result = subprocess.run(
            [NVIDIA_SMI, f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            text=True,
            capture_output=True,
            timeout=8,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "unavailable", "error": str(exc), "devices": []}
    if result.returncode != 0:
        return {
            "status": "unavailable",
            "error": (result.stderr or result.stdout or "nvidia-smi failed")[:300],
            "devices": [],
        }

    devices: list[dict[str, Any]] = []
    by_uuid: dict[str, dict[str, Any]] = {}
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 9:
            continue
        total_mib, used_mib, free_mib = (_number(parts[i]) for i in (3, 4, 5))
        device = {
            "index": int(_number(parts[0]) or 0),
            "uuid": parts[1],
            "name": parts[2],
            "memory_total_bytes": int((total_mib or 0) * 1024**2),
            "memory_used_bytes": int((used_mib or 0) * 1024**2),
            "memory_free_bytes": int((free_mib or 0) * 1024**2),
            "memory_used_percent": round((used_mib or 0) / total_mib * 100, 1) if total_mib else None,
            "utilization_percent": _number(parts[6]),
            "temperature_c": _number(parts[7]),
            "power_w": _number(parts[8]),
            # "Active" only when the card is actually being throttled by heat
            "thermal_slowdown": (
                parts[9].strip().lower() == "active" if len(parts) > 9 else None
            ),
            "processes": [],
        }
        devices.append(device)
        by_uuid[parts[1]] = device

    try:
        processes = subprocess.run(
            [
                NVIDIA_SMI,
                "--query-compute-apps=gpu_uuid,pid,process_name,used_memory",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            capture_output=True,
            timeout=8,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if processes.returncode == 0:
            for line in processes.stdout.splitlines():
                parts = [part.strip() for part in line.split(",", 3)]
                if len(parts) != 4 or parts[0] not in by_uuid:
                    continue
                used_mib = _number(parts[3])
                by_uuid[parts[0]]["processes"].append(
                    {
                        "pid": int(_number(parts[1]) or 0),
                        "name": Path(parts[2]).name,
                        "memory_used_bytes": int(used_mib * 1024**2) if used_mib is not None else None,
                    }
                )
    except (OSError, subprocess.TimeoutExpired):
        pass

    return {
        "status": "healthy" if devices else "unavailable",
        "device_count": len(devices),
        "memory_total_bytes": sum(item["memory_total_bytes"] for item in devices),
        "memory_used_bytes": sum(item["memory_used_bytes"] for item in devices),
        "memory_free_bytes": sum(item["memory_free_bytes"] for item in devices),
        "devices": devices,
    }


def _strata_metrics() -> dict[str, Any] | None:
    """Live engine telemetry from /metrics, or None when the engine is unreachable.

    The inference block used to be config-derived only (backend + model count), which
    cannot show a busy engine, a prefill in progress, or a conversation that was
    evicted instead of parked. Every key here is optional: when the probe fails the
    caller leaves the block exactly as it was.
    """
    try:
        runtime = get_runtime_config()
        if runtime.get("status") != "healthy":
            return None
        request = Request(
            runtime["base_url"] + "/metrics",
            headers={**authorization_headers(runtime), "User-Agent": "HermesHealthApi/1.0"},
        )
        with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            payload = json.load(response)
        live = payload.get("live") or {}
        eng = payload.get("engine") or {}
        cache = payload.get("conversation_cache") or {}
        totals = payload.get("totals") or {}
        return {
            "live": {
                "state": live.get("state"), "phase": live.get("phase"),
                "running": live.get("running"), "queued": live.get("queued"),
                "parallel": live.get("parallel"), "waiting": live.get("waiting"),
                "tok_s": live.get("tok_s"), "tok_s_mean": live.get("tok_s_mean"),
                "prefill_tok_s_mean": live.get("prefill_tok_s_mean"),
                "generated": live.get("generated"), "elapsed_s": live.get("elapsed_s"),
            },
            "engine": {
                "model": eng.get("model"), "context": eng.get("max_context"),
                "kv": eng.get("kv"), "vram_free_mib": eng.get("vram_free_mib"),
                "expert_cache_mib": eng.get("expert_cache_mib"),
                "batch_slots": eng.get("batch_slots"), "batch_groups": eng.get("batch_groups"),
            },
            "conversation_cache": {
                "enabled": cache.get("enabled"), "slots": cache.get("slots"),
                "parked": cache.get("parked"), "evictions": cache.get("evictions"),
                "parks": cache.get("parks"), "restores": cache.get("restores"),
                "bytes": cache.get("bytes"), "budget_mib": cache.get("budget_mib"),
                "last_event": cache.get("last_event"),
                "reused_tokens": cache.get("reused_tokens"),
                "requests_reused": cache.get("requests_reused"),
            },
            "totals": {
                "requests": totals.get("requests"), "output_tokens": totals.get("output_tokens"),
                "prompt_tokens": totals.get("prompt_tokens"), "reused": totals.get("reused"),
            },
        }
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def llama_cpp_details() -> dict[str, Any]:
    processes: list[dict[str, Any]] = []
    effective_context_size: int | None = None
    try:
        slots = []
        runtime = get_runtime_config()
        if runtime.get("status") == "healthy":
            request = Request(
                runtime["base_url"] + "/slots",
                headers={**authorization_headers(runtime), "User-Agent": "HermesHealthApi/1.0"},
            )
            with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                slots = json.load(response)
        slot_contexts = [int(slot["n_ctx"]) for slot in slots if isinstance(slot, dict) and slot.get("n_ctx")]
        if slot_contexts:
            effective_context_size = max(slot_contexts)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    try:
        for process in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
            if (process.info.get("name") or "").lower() not in {"llama-server", "llama-server.exe"}:
                continue
            command = list(process.info.get("cmdline") or [])

            def option(*names: str) -> str | None:
                for name in names:
                    try:
                        return command[command.index(name) + 1]
                    except (ValueError, IndexError):
                        continue
                return None

            model = option("--model", "-m")
            mmproj = option("--mmproj")
            memory = process.memory_info()
            processes.append(
                {
                    "pid": process.info["pid"],
                    "memory_rss_bytes": memory.rss,
                    "memory_private_bytes": getattr(memory, "private", None),
                    "model": Path(model).name if model else "unknown",
                    "model_path": model,
                    "vision_encoder": Path(mmproj).name if mmproj else None,
                    "context_size": option("--ctx-size", "-c"),
                    "effective_context_size": effective_context_size,
                    "context_metadata_override": option("--override-kv"),
                    "cache_type_k": option("--cache-type-k", "-ctk"),
                    "cache_type_v": option("--cache-type-v", "-ctv"),
                    "draft_cache_type_k": option("--spec-draft-type-k"),
                    "draft_cache_type_v": option("--spec-draft-type-v"),
                    "tensor_split": option("--tensor-split"),
                    "batch_size": option("--batch-size", "-b"),
                    "ubatch_size": option("--ubatch-size", "-ub"),
                    "flash_attention": option("--flash-attn", "-fa"),
                    "started_at": iso_utc(datetime.fromtimestamp(float(process.info["create_time"]), timezone.utc)),
                }
            )
    except (psutil.Error, OSError, TypeError, ValueError):
        pass
    if not processes:
        runtime = get_runtime_config()
        if runtime.get("status") == "healthy":
            config = safe_server_config(runtime)
            processes.append({
                "pid": None,
                "memory_rss_bytes": None,
                "model": runtime.get("model"),
                "model_path": None,
                "vision_encoder": "cpu",
                "context_size": config.get("context_size"),
                "effective_context_size": effective_context_size,
                "cache_type_k": config.get("kv"),
                "cache_type_v": config.get("kv"),
                "tensor_split": config.get("gpu_roles") or config.get("gpu"),
                "batch_size": config.get("batch_slots"),
                "ubatch_size": None,
                "flash_attention": "on",
                "started_at": None,
                "backend": "strata",
            })
    result = {
        "backend": "strata" if any(p.get("backend") == "strata" for p in processes) else "llama.cpp",
        "status": "healthy" if processes else "unhealthy",
        "loaded_model_count": len(processes),
        "processes": processes,
    }
    # Additive: live engine numbers when /metrics answers. Absent probe -> block unchanged.
    live = _strata_metrics()
    if live:
        result.update(live)
    return result


# ── Inference activity: what the serving model is doing right now ──────────────
# Additive to the live GPU snapshot. Every probe is isolated so an unreachable
# inference server or Hindsight can never blank the GPU telemetry the dashboard
# already renders.
ACTIVITY_TIMEOUT_SECONDS = 2
HINDSIGHT_QUEUE_TTL_SECONDS = 10
HINDSIGHT_BANK = "hermes"
# What each slot is serving. The inference API itself does not name its clients, so we
# resolve labels out-of-band, all cached behind short TTLs and only on success:
#   - per-client label (process identity): 10 s TTL, cheap psutils rescan;
#   - active Hermes WebUI session title: 10 s TTL, one stat + one small JSON read of the
#     most recently updated session file - the WebUI touches its own file on every turn;
#   - Hindsight operation labels (API queue + Postgres llm_requests): 10 s TTL.
ACTIVITY_CLIENT_TTL_SECONDS = 10
ACTIVITY_SESSION_TTL_SECONDS = 10
ACTIVITY_HINDSIGHT_OPS_TTL_SECONDS = 10
WEBUI_SESSIONS_DIR = Path.home() / ".hermes" / "webui" / "sessions"
CLIENT_LABELS = {
    "hindsight-api": "Hindsight memory",
    "hermes-webui-server.py": "Hermes WebUI",
}
KNOWN_CLIENT_NAMES = {"python", "python3"}
ACTIVITY_STATE: dict[str, Any] = {
    "hindsight": None,
    "hindsight_at": None,
    "server": None,
    "server_at": None,
    "vram": None,
    "vram_at": None,
}

# nvidia-smi reports clocks_event_reasons.active as a bitmask. Decode it to names so the
# panel can show *why* a card is capped (e.g. the stock 170 W power cap) instead of "capped".
THROTTLE_BITS = {
    "gpu_idle": 0x1, "applications_clocks": 0x2, "sw_power_cap": 0x4, "hw_slowdown": 0x8,
    "sync_boost": 0x10, "sw_thermal": 0x20, "hw_thermal": 0x40, "hw_power_brake": 0x80,
    "display_clock": 0x100,
}
SERVER_CONFIG_TTL_SECONDS = 300
VRAM_BUDGET_TTL_SECONDS = 600
VRAM_PATTERNS = {
    "model": re.compile(r"(CUDA\d) model buffer size =\s*([0-9.]+) MiB"),
    "kv": re.compile(r"(CUDA\d) KV buffer size =\s*([0-9.]+) MiB"),
    "recurrent": re.compile(r"(CUDA\d) RS buffer size =\s*([0-9.]+) MiB"),
    "compute": re.compile(r"(CUDA\d) compute buffer size =\s*([0-9.]+) MiB"),
}


def _throttle_reasons(mask: Any) -> list[str]:
    """Decode the nvidia-smi throttle bitmask. Blank or unparseable input yields no reasons."""
    try:
        value = int(str(mask).strip(), 16)
    except (TypeError, ValueError):
        return []
    return [name for name, bit in THROTTLE_BITS.items() if value & bit]


# Launch flags that cannot change without restarting the engine. The dashboard needs them (context
# size, KV type, layer split, parking floor, spec/MTP settings) but they are not live values, so the
# API publishes them as one static block instead of letting them sit beside live rows.
ENGINE_VALUE_FLAGS = {
    "--max-context": "max_context",
    "--kv": "kv",
    "--batch": "batch",
    "--batch-groups": "batch_groups",
    "--spec": "spec",
    "--spec-min-p": "spec_min_p",
    "--mtp-q4": "mtp_q4",
    "--mtp-max": "mtp_max",
    "--layer-split": "layer_split",
    "--vram-reserve-mib": "vram_reserve_mib",
    "--conversation-cache-mib": "conversation_cache_mib",
    "--conversation-cache-slots": "conversation_cache_slots",
    "--conversation-cache-min-free-mib": "conversation_cache_min_free_mib",
    "--conversation-cache-park-gain": "conversation_cache_park_gain",
    "--prompt-cache": "prompt_cache",
    "--lookup-chain": "lookup_chain",
    "--pipeline-windows": "pipeline_windows",
    "--ple-row_cache": "ple_row_cache",
    "--ple-row-cache": "ple_row_cache",
    "--adapt-min-gain": "adapt_min_gain",
    "--expert-cache": "expert_cache",
    "--expert-profile": "expert_profile",
    "--native": "native_gguf",
    "--ple-gguf": "ple_gguf",
}
ENGINE_BOOL_FLAGS = {"--batch-mtp", "--prompt-cache-spread", "--trim-stage-weights",
                     "--session-save-reclaim", "--vision"}


def _engine_pack() -> dict[str, Any]:
    """The pack the running engine actually loaded, its quant, and its fixed launch flags.

    The profile's `model:`/`pack:` strings are not the truth (the profile still carried the deleted
    IQ3_S build and the Q4_K_XL pack while the box serves UD-IQ4_XS), so This reads the engine's own command
    line instead — the same source the rest of the stack trusts — and reports the launch flags verbatim
    rather than re-deriving them from a config file.
    """
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % entry, "rb") as fh:
                    argv = fh.read().split(b"\0")
            except OSError:
                continue
            argv = [a.decode("utf-8", "replace") for a in argv if a]
            if not any(a.endswith("engine/strata") or a == "strata" for a in argv):
                continue
            if "--pack" not in argv:
                continue
            pack = argv[argv.index("--pack") + 1]
            name = pack.rstrip("/").rsplit("/", 1)[-1]
            quant = {
                "iq4_xs": "IQ4_XS", "iq4_nl": "IQ4_NL", "q4_k_xl": "Q4_K_XL", "q3_k_xl": "Q3_K_XL",
                "iq3_xxs": "IQ3_XXS", "iq3_s": "IQ3_S", "iq2_xs": "IQ2_XS",
            }.get(name, name.upper())
            flags: dict[str, Any] = {}
            for index, token in enumerate(argv):
                key = ENGINE_VALUE_FLAGS.get(token)
                if key is None:
                    continue
                raw = argv[index + 1] if index + 1 < len(argv) else ""
                if not raw or raw.startswith("-"):
                    continue
                try:
                    flags[key] = int(raw)
                except ValueError:
                    flags[key] = raw
            for token in argv:
                if token in ENGINE_BOOL_FLAGS:
                    flags[token[2:].replace("-", "_")] = True
            return {"pack": pack, "quant": quant, "engine_flags": flags}
    except OSError:
        pass
    return {}


def _llama_server_config() -> dict[str, Any]:
    """Read active runtime flags through its bounded 10-second cache, plus the served pack."""
    config = dict(safe_server_config() or {})
    config.update(_engine_pack())
    return config

def _vram_budget() -> dict[str, Any]:
    """Where the VRAM went: model / KV / recurrent / compute buffers per GPU.

    Parsed from the container's startup allocation lines (they only change on a restart, so
    the result is cached for ten minutes). `head` closes the pipe early, so streaming the
    log start stays cheap even on a long log.
    """
    now = time.monotonic()
    cached, cached_at = ACTIVITY_STATE.get("vram"), ACTIVITY_STATE.get("vram_at")
    if isinstance(cached, dict) and isinstance(cached_at, (int, float)) \
            and now - cached_at < VRAM_BUDGET_TTL_SECONDS:
        return cached
    result: dict[str, Any] = {"status": "unavailable"}
    try:
        completed = subprocess.run(
            ["sh", "-c", f"docker logs {LLAMA_CONTAINER} 2>&1 | head -600"],
            capture_output=True, text=True, timeout=8, check=False,
        )
        text = completed.stdout or ""
        buffers: dict[str, dict[str, float]] = {}
        for kind, pattern in VRAM_PATTERNS.items():
            for device, value in pattern.findall(text):
                buffers.setdefault(device, {}).setdefault(kind, float(value))
        if buffers:
            for device, entries in buffers.items():
                entries["total_mib"] = round(sum(v for k, v in entries.items() if k != "total_mib"), 1)
            result = {"status": "healthy", "source": "container startup allocation lines",
                      "buffers": buffers}
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    # Cache successes only - see the note in _llama_server_config.
    if result.get("status") == "healthy":
        ACTIVITY_STATE["vram"], ACTIVITY_STATE["vram_at"] = result, now
    return result


def _llama_metrics() -> dict[str, float]:
    """Cumulative llama.cpp counters from /metrics. Empty when unreachable."""
    runtime = get_runtime_config()
    if runtime.get("status") != "healthy":
        return {}
    request = Request(
        runtime["base_url"] + "/metrics",
        headers={**authorization_headers(runtime), "User-Agent": "HermesHealthApi/1.0"},
    )
    with urlopen(request, timeout=ACTIVITY_TIMEOUT_SECONDS) as response:
        text = response.read().decode("utf-8", "replace")
    counters: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#") or " " not in line:
            continue
        name, _, raw = line.rpartition(" ")
        if not name.startswith("llamacpp:"):
            continue
        try:
            counters[name.split(":", 1)[1]] = float(raw)
        except ValueError:
            continue
    return counters


# Live generation progress is only ever printed to the server log; /metrics counters
# advance only when a request *completes*, and its rate gauges read 0 mid-request.
GENERATION_RE = re.compile(
    r"print_timing: id\s+(\d+) \| task (\d+) \| n_gen =\s*(\d+), "
    r"tg =\s*([0-9.]+) t/s, tg_3s =\s*([0-9.]+) t/s")
PREFILL_RE = re.compile(
    r"print_timing: id\s+(\d+) \| task (\d+) \| prompt eval time =\s*([0-9.]+) ms /\s*"
    r"(\d+) tokens \([^)]*?([0-9.]+) tokens per second\)")
# llama.cpp prints the host-side prompt cache state whenever it updates, e.g.
#   cache state: 13 prompts, 8423.995 MiB (limits: 28672.000 MiB, 320000 tokens, 344031 est)
# With --cache-ram -1 the MiB limit is reported as 0.000 (or the limit section may be
# absent), so a non-positive limit means "no limit" rather than a real zero.
PROMPT_CACHE_RE = re.compile(
    r"cache state:\s*(\d+)\s*prompts?,\s*([0-9.]+)\s*MiB"
    r"(?:\s*\(limits:\s*([0-9.]+)\s*MiB,\s*(\d+)\s*tokens,\s*(\d+)\s*est\))?")


STRATA_ENGINE_LOG = "/home/clara/local-inference/strata-src/strata-iq4xs.log"
PARKED_BYTES_RE = re.compile(r"bytes=(\d+) evictions=")   # the parked set, not snapshot_bytes/reused_kv_bytes


def _strata_parked_bytes() -> int | None:
    """Bytes held by the parked-conversation set, from the engine's last park line.

    Strata writes one line per park ("... parked=3 bytes=4296080512"), so the freshest such line
    is the live size of the cache. None when the log cannot be read; 0 when nothing is parked yet,
    which is the truth right after a restart.
    """
    try:
        with open(STRATA_ENGINE_LOG, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 400_000))
            chunk = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    found = None
    for line in chunk.splitlines():
        if "conversation cache: parked" in line:
            match = PARKED_BYTES_RE.search(line)
            if match:
                found = int(match.group(1))
    return found if found is not None else 0


def _recent_server_log(tail: int = 40) -> str:
    try:
        result = subprocess.run(
            ["docker", "logs", "--tail", str(tail), LLAMA_CONTAINER],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (result.stdout or "") + (result.stderr or "")


def _live_generation() -> dict[str, Any] | None:
    """Newest in-flight generation line: task, tokens so far, current decode rate."""
    for line in reversed(_recent_server_log().splitlines()):
        match = GENERATION_RE.search(line)
        if match:
            return {
                "slot_id": int(match.group(1)),
                "task_id": int(match.group(2)),
                "generated_tokens": int(match.group(3)),
                "decode_tokens_per_second": float(match.group(4)),
                "decode_tokens_per_second_3s": float(match.group(5)),
            }
    return None


def _last_prefill_rate() -> dict[str, Any] | None:
    """Most recent completed prompt-processing rate, with its token count."""
    for line in reversed(_recent_server_log().splitlines()):
        match = PREFILL_RE.search(line)
        if match:
            return {
                "task_id": int(match.group(2)),
                "prompt_tokens": int(match.group(4)),
                "prefill_tokens_per_second": float(match.group(5)),
            }
    return None


PROMPT_CACHE_TTL_SECONDS = 10


CONTROL_PATH = Path.home() / ".hermes" / "state" / "hindsight-control.json"

# Jobs the dashboard's Pause button controls, by name (ids are resolved from jobs.json).
CONTROL_JOB_NAMES = (
    "hindsight-consolidation-window",
    "hindsight-backfill-window",
    "line-hindsight-midnight-flush",
    "line-hindsight-midnight-sweep",
)
# Hindsight plugin configs the Pause button flips (auto_retain off/on).
HINDSIGHT_CONFIGS = (
    Path.home() / ".hermes" / "hindsight" / "config.json",
    Path.home() / ".hermes" / "profiles" / "line-listener" / "hindsight" / "config.json",
)


def _hermes_env() -> dict[str, str]:
    """Run the CLI against the DEFAULT profile.

    The shell here pins HERMES_HOME to a profile dir, so a bare `hermes cron ...` edits the
    wrong profile's jobs.json (verified 2026-10-16: it wrote into `cad`). Force it.
    """
    env = dict(os.environ)
    env["HERMES_HOME"] = str(Path.home() / ".hermes")
    env.pop("HERMES_PROFILE", None)
    return env


def _read_control() -> dict[str, Any]:
    try:
        data = json.loads(CONTROL_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def control_state() -> dict[str, Any]:
    """Pause state for the dashboard: paused + hours left."""
    st = _read_control()
    left = int(st.get("until_epoch", 0) - time.time())
    paused = bool(st.get("paused")) and left > 0
    return {
        "paused": paused,
        "hours": st.get("hours") if paused else None,
        "paused_at": st.get("paused_at") if paused else None,
        "remaining_seconds": left if paused else 0,
        "remaining_human": ("%dh %02dm" % (left // 3600, (left % 3600) // 60)) if paused else None,
    }


def _set_auto_retain(flag: bool) -> list[str]:
    """Flip the Hindsight plugin's inline retain. Fails soft: a bad file is skipped, not fatal."""
    changed: list[str] = []
    for path in HINDSIGHT_CONFIGS:
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        if data.get("auto_retain") is flag:
            continue
        data["auto_retain"] = flag
        try:
            path.write_text(json.dumps(data, indent=2) + "\n")
            changed.append(str(path))
        except Exception:
            continue
    return changed


def _control_jobs() -> dict[str, str]:
    try:
        doc = json.loads((Path.home() / ".hermes" / "cron" / "jobs.json").read_text())
    except Exception:
        return {}
    jobs = doc.get("jobs", doc) if isinstance(doc, dict) else doc
    out: dict[str, str] = {}
    for job in jobs or []:
        name = job.get("name") or ""
        if name in CONTROL_JOB_NAMES and job.get("id"):
            out[name] = job["id"]
    return out


def _set_crons(enabled: bool) -> list[str]:
    verb = "resume" if enabled else "pause"
    done: list[str] = []
    for name, jid in _control_jobs().items():
        try:
            subprocess.run(["hermes", "cron", verb, jid], env=_hermes_env(),
                           capture_output=True, text=True, timeout=60)
            done.append(name)
        except Exception:
            continue
    return done


def _prompt_cache_state() -> dict[str, Any]:
    """Host RAM held by the engine's prompt cache, parsed from its own log.

    The engine prints `cache state: N prompts, X MiB (limits: ...)` whenever the
    cache updates; the freshest such line is the live snapshot. Cached for 10 s and
    successes only, so the 2 s sampler does not re-run docker logs every cycle while
    a failure (container restarting, docker slow) does not stick for the TTL.
    """
    now = time.monotonic()
    cached, cached_at = ACTIVITY_STATE.get("prompt_cache"), ACTIVITY_STATE.get("prompt_cache_at")
    if isinstance(cached, dict) and isinstance(cached_at, (int, float)) \
            and now - cached_at < PROMPT_CACHE_TTL_SECONDS:
        return cached
    unavailable = {
        "status": "unavailable",
        "prompts": None,
        "mib": None,
        "limit_mib": None,
        "tokens_limit": None,
        "tokens_est": None,
        "source": "engine log (cache state line)",
        "updated_at": iso_utc(),
    }
    # NOTE: a large tail is required here. The generation line is logged constantly, so the
    # default small window finds it — but "cache state" is only logged when the cache UPDATES
    # (measured: 7 lines in 30 min). With --tail 200 this returned 0 matches and the panel sat
    # on "unavailable" forever; at 3000 it finds ~24. Measured 2026-10-14.
    for line in reversed(_recent_server_log(tail=3000).splitlines()):
        match = PROMPT_CACHE_RE.search(line)
        if match:
            limit_mib = float(match.group(3)) if match.group(3) else None
            result = {
                "status": "healthy",
                "prompts": int(match.group(1)),
                "mib": round(float(match.group(2)), 1),
                # -1 (no limit) reports 0.000 MiB on the limits line: not a real zero.
                "limit_mib": limit_mib if (limit_mib is not None and limit_mib > 0) else None,
                "tokens_limit": int(match.group(4)) if match.group(4) else None,
                "tokens_est": int(match.group(5)) if match.group(5) else None,
                "source": "engine log (cache state line)",
                "updated_at": iso_utc(),
            }
            ACTIVITY_STATE["prompt_cache"], ACTIVITY_STATE["prompt_cache_at"] = result, now
            return result
    return unavailable


def _slot_clients() -> list[dict[str, Any]]:
    """Local processes holding an established connection to the inference API."""
    try:
        connections = psutil.net_connections(kind="tcp")
    except (psutil.Error, OSError):
        return []
    grouped: dict[int, dict[str, Any]] = {}
    own_pid = os.getpid()
    for connection in connections:
        remote = getattr(connection, "raddr", None)
        if not remote or getattr(remote, "port", None) != 8080:
            continue
        if connection.status != psutil.CONN_ESTABLISHED:
            continue
        pid = connection.pid
        if not pid or pid == own_pid:
            continue   # do not report our own health probes as a serving client
        entry = grouped.setdefault(pid, {"pid": pid, "name": None, "connections": 0})
        entry["connections"] += 1
    for entry in grouped.values():
        try:
            entry["name"] = psutil.Process(entry["pid"]).name()
        except (psutil.Error, OSError):
            entry["name"] = None
    return sorted(grouped.values(), key=lambda item: item["pid"])


def _client_labels(clients: list[dict[str, Any]]) -> dict[str, Any]:
    """Human-readable identity per connected client: pid, command, role label.

    Cached for 10 s and only on success, so the 2 s sampler does not re-walk
    /proc for every client on every cycle.
    """
    now = time.monotonic()
    cached, cached_at = ACTIVITY_STATE.get("client_labels"), ACTIVITY_STATE.get("client_labels_at")
    if isinstance(cached, dict) and isinstance(cached_at, (int, float)) \
            and now - cached_at < ACTIVITY_CLIENT_TTL_SECONDS:
        return cached
    resolved: dict[str, Any] = {}
    for client in clients:
        pid = client.get("pid")
        if not isinstance(pid, int):
            continue
        process = psutil.Process(pid)
        try:
            with process.oneshot():
                process_name = process.name()
                command = " ".join(process.cmdline())
        except (psutil.Error, OSError):
            process_name, command = None, None
        label = next((friendly for key, friendly in CLIENT_LABELS.items()
                      if key in (process_name or "") or key in (command or "")), None)
        resolved[str(pid)] = {
            "cmdline": command or None,
            "label": label,
            "role": "hindsight" if label == "Hindsight memory" else None,
        }
    ACTIVITY_STATE["client_labels"], ACTIVITY_STATE["client_labels_at"] = resolved, now
    return resolved


def _active_session_title() -> dict[str, Any]:
    """Most recent Hermes WebUI session title: the WebUI touches its own session
    file on every turn, so the newest mtime is the session driving inference right
    now. Cached for 10 s; one os.stat + one small JSON read, successes only."""
    now = time.monotonic()
    cached, cached_at = ACTIVITY_STATE.get("session"), ACTIVITY_STATE.get("session_at")
    if isinstance(cached, dict) and isinstance(cached_at, (int, float)) \
            and now - cached_at < ACTIVITY_SESSION_TTL_SECONDS:
        return cached
    result: dict[str, Any] = {"title": None, "id": None}
    try:
        newest, newest_mtime = None, 0.0
        for filename in os.listdir(WEBUI_SESSIONS_DIR):
            if not filename.endswith(".json") or filename.startswith("_"):
                continue   # _index.json, _deleted_*.json are bookkeeping, not sessions
            path = WEBUI_SESSIONS_DIR / filename
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime > newest_mtime:
                newest, newest_mtime = path, mtime
        if newest is not None:
            # Only a session touched just now is the one driving inference. WebUI session
            # files can carry a future mtime (clock/timezone skew), which previously surfaced
            # a stale title on an idle slot or on a CLI run that never touches these files.
            age = time.time() - newest_mtime
            if 0 <= age <= 120:
                details = json.loads(newest.read_text(encoding="utf-8"))
                # The newest file could theoretically be a non-session JSON; guard it.
                if isinstance(details, dict) and isinstance(details.get("title"), str):
                    result = {"title": details["title"], "id": details.get("session_id")}
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    if result.get("title"):
        ACTIVITY_STATE["session"], ACTIVITY_STATE["session_at"] = result, now
    return result


def _hindsight_operation_label() -> dict[str, Any]:
    """Human-readable label for what Hindsight is currently running.

    Primary: the Hindsight API's own queue (operation type + status). Secondary:
    the latest llm_requests row in Hindsight's own Postgres, which carries scope text.
    Cached for 10 s; successes only - a failed probe must not stick for the TTL.
    """
    now = time.monotonic()
    cached, cached_at = ACTIVITY_STATE.get("hindsight_ops"), ACTIVITY_STATE.get("hindsight_ops_at")
    if isinstance(cached, dict) and isinstance(cached_at, (int, float)) \
            and now - cached_at < ACTIVITY_HINDSIGHT_OPS_TTL_SECONDS:
        return cached
    result: dict[str, Any] = {"status": "unavailable"}
    try:
        request = Request(
            "http://127.0.0.1:8888/v1/default/banks/hermes/operations?limit=40",
            headers={"User-Agent": "HermesHealthApi/1.0"},
        )
        with urlopen(request, timeout=ACTIVITY_TIMEOUT_SECONDS) as response:
            operations = json.load(response).get("operations") or []
    except (HTTPError, URLError, TimeoutError, OSError, ValueError,
            TypeError, json.JSONDecodeError):
        operations = []
    active = next((op for op in operations if isinstance(op, dict)
                   and op.get("status") in {"processing", "pending"}), None)
    if active is not None:
        result = {
            "status": "healthy",
            "operation": active.get("task_type"),
            "scope": (active.get("details") or (f"{active.get('items_count')} item(s)"
                      if active.get("items_count") is not None else None)) or None,
            "source": "hindsight-api queue",
        }
    else:
        # Queue idle - fall back to the newest llm_requests row for scope context.
        scope = _hindsight_postgres_scope()
        if scope:
            result = {"status": "healthy", "operation": scope["operation"],
                      "scope": scope["scope"], "source": "hindsight Postgres llm_requests"}
        elif operations:
            result = {"status": "healthy", "operation": None, "scope": None,
                      "source": "hindsight-api queue (idle)"}
    if result.get("status") == "healthy" and (result["operation"] or result["scope"]):
        ACTIVITY_STATE["hindsight_ops"], ACTIVITY_STATE["hindsight_ops_at"] = result, now
    return result


def _hindsight_postgres_scope() -> dict[str, Any] | None:
    """Newest llm_requests row in Hindsight's own Postgres: operation + scope text.

    Single docker exec + psql, 2 s timeout, called only when the API queue is idle.
    """
    command_string = (
        "docker exec hindsight sh -lc "
        "'export LD_LIBRARY_PATH=/home/hindsight/.pg0/installation/18.1.0/lib; "
        "PGPASSWORD=hindsight /home/hindsight/.pg0/installation/18.1.0/bin/psql "
        "-h 127.0.0.1 -U hindsight -d hindsight -tAc \""
        "SELECT operation, scope, left(coalesce(metadata::text,''),80) "
        "FROM llm_requests ORDER BY started_at DESC LIMIT 1\"'"
    )
    try:
        completed = subprocess.run(
            ["sh", "-c", command_string],
            capture_output=True, text=True, timeout=ACTIVITY_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    row = (completed.stdout or "").strip()
    if not row or "| " in row[:2]:   # psql prints "psql:<error>" lines too; skip them
        return None
    parts = [part.strip() for part in row.split("|", 2)]
    if len(parts) < 2 or not parts[0]:
        return None
    return {"operation": parts[0], "scope": parts[1] or None}


def _hindsight_queue() -> dict[str, Any]:
    """In-flight Hindsight operations, cached: the queue moves slower than 2 s."""
    now = time.monotonic()
    cached, cached_at = ACTIVITY_STATE.get("hindsight"), ACTIVITY_STATE.get("hindsight_at")
    if isinstance(cached, dict) and isinstance(cached_at, (int, float)) \
            and now - cached_at < HINDSIGHT_QUEUE_TTL_SECONDS:
        return cached
    result: dict[str, Any] = {"status": "unavailable", "bank": HINDSIGHT_BANK}
    try:
        request = Request(
            f"http://127.0.0.1:8888/v1/default/banks/{HINDSIGHT_BANK}/operations?limit=40",
            headers={"User-Agent": "HermesHealthApi/1.0"},
        )
        with urlopen(request, timeout=ACTIVITY_TIMEOUT_SECONDS) as response:
            operations = json.load(response).get("operations") or []
        counts: dict[str, int] = {}
        in_flight: dict[str, int] = {}
        for operation in operations:
            if not isinstance(operation, dict):
                continue
            state = str(operation.get("status") or "unknown")
            counts[state] = counts.get(state, 0) + 1
            if state in {"processing", "pending"}:
                task_type = str(operation.get("task_type") or "unknown")
                in_flight[task_type] = in_flight.get(task_type, 0) + 1
        result = {
            "status": "healthy",
            "bank": HINDSIGHT_BANK,
            "sampled": len(operations),
            "processing": counts.get("processing", 0),
            "pending": counts.get("pending", 0),
            "failed": counts.get("failed", 0),
            "completed": counts.get("completed", 0),
            "in_flight_by_type": in_flight,
            "checked_at": iso_utc(),
            "checked_at_epoch": int(time.time()),
        }
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        # Down must be visible as down, with the reason the API actually has. An empty card reads as
        # "fine", which is the bug this card was filed about.
        result = {"status": "unavailable", "bank": HINDSIGHT_BANK,
                  "reason": f"{type(exc).__name__}: {exc}"[:200],
                  "checked_at": iso_utc(), "checked_at_epoch": int(time.time())}
    ACTIVITY_STATE["hindsight"], ACTIVITY_STATE["hindsight_at"] = result, now
    return result


def _up(url: str, timeout: float = 6.0) -> bool:
    try:
        with urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except Exception:
        return False


def _job_enabled(job_id: str) -> Any:
    """True/False/None for a cron job, read straight from jobs.json."""
    try:
        doc = json.loads((Path.home() / ".hermes" / "cron" / "jobs.json").read_text())
    except Exception:
        return None
    jobs = doc.get("jobs", doc) if isinstance(doc, dict) else doc
    for job in jobs or []:
        if job.get("id") == job_id:
            return job.get("enabled")
    return None


def pause_hindsight(hours: Any) -> dict[str, Any]:
    """Pause Hindsight's GPU work, remembering exactly what changed so resume can undo *that*.

    Not everything should come back as true: the line-listener's per-profile config carries
    auto_retain=false on purpose (its ingestion runs through the 22:00 flush instead).
    """
    hours = max(1, min(720, int(hours or 1)))
    prev_auto: list[str] = []
    for path in HINDSIGHT_CONFIGS:
        try:
            if json.loads(path.read_text()).get("auto_retain") is True:
                prev_auto.append(str(path))
        except Exception:
            continue
    prev_jobs: list[str] = []
    for name, jid in _control_jobs().items():
        if _job_enabled(jid) is not False:
            prev_jobs.append(jid)
    CONTROL_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONTROL_PATH.write_text(json.dumps({
        "paused": True, "hours": hours, "paused_at": iso_utc(),
        "until_epoch": time.time() + hours * 3600,
        "auto_retain_was_true": prev_auto, "jobs_paused": prev_jobs,
    }, indent=2) + "\n")
    return {"ok": True, "paused": True, "hours": hours,
            "auto_retain_disabled": _set_auto_retain(False),
            "crons_paused": _set_crons(False),
            "state": control_state()}


def resume_hindsight() -> dict[str, Any]:
    """Undo a pause exactly: restore only the configs and jobs that the pause changed."""
    st = _read_control()
    restored: list[str] = []
    for path in st.get("auto_retain_was_true") or []:
        try:
            data = json.loads(Path(path).read_text())
            if data.get("auto_retain") is True:
                continue
            data["auto_retain"] = True
            Path(path).write_text(json.dumps(data, indent=2) + "\n")
            restored.append(path)
        except Exception:
            continue
    resumed: list[str] = []
    for jid in st.get("jobs_paused") or []:
        try:
            subprocess.run(["hermes", "cron", "resume", jid], env=_hermes_env(),
                           capture_output=True, text=True, timeout=60)
            resumed.append(jid)
        except Exception:
            continue
    CONTROL_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONTROL_PATH.write_text(json.dumps({"paused": False, "resumed_at": iso_utc()}, indent=2) + "\n")
    return {"ok": True, "paused": False, "auto_retain_restored": restored,
            "crons_resumed": resumed, "state": control_state()}


def digest_now() -> dict[str, Any]:
    """Run Hindsight's pipeline now: consolidate, flush today's LINE ingest, one capsule."""
    runtime = get_runtime_config(force_refresh=True)
    if runtime.get("status") != "healthy":
        return {"ok": False, "dispatched": [],
                "error": "Strata is not loaded on 8080: %s" % runtime.get("error", "unavailable")}
    if not _up("http://127.0.0.1:8888/health"):
        return {"ok": False, "dispatched": [], "error": "Hindsight API is not answering on 8888."}
    out: dict[str, Any] = {"ok": True, "dispatched": [], "failed": []}
    try:
        req = Request(
            "http://127.0.0.1:8888/v1/default/banks/hermes/consolidate",
            data=b"{}", method="POST", headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=120):
            out["dispatched"].append("consolidate")
    except Exception as exc:  # noqa: BLE001 - report, never raise into the dashboard
        out["failed"].append("consolidate: %s" % exc)
    home = Path.home()
    runs = (
        ("line-flush", [str(home / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"),
                        str(home / ".hermes" / "scripts" / "line-hindsight-midnight-flush.py"),
                        "run", time.strftime("%Y-%m-%d")]),
    )
    for label, cmd in runs:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=420)
            if proc.returncode == 0:
                out["dispatched"].append(label)
            else:
                out["failed"].append("%s: rc=%s" % (label, proc.returncode))
        except Exception as exc:  # noqa: BLE001
            out["failed"].append("%s: %s" % (label, exc))
    return out


def _strata_live() -> dict[str, Any] | None:
    """The engine's own live block, or None when it is not answering.

    Strata reports real progress in /metrics -> live (prompt_read / prompt_total while
    reading, generated / max_tokens while generating). Its /slots is llama.cpp-shaped
    but carries no n_prompt_tokens_processed, so a bar built from /slots alone is
    always 0% wide.
    """
    runtime = get_runtime_config()
    if runtime.get("status") != "healthy":
        return None
    try:
        request = Request(runtime["base_url"] + "/metrics",
                          headers={**authorization_headers(runtime),
                                   "User-Agent": "HermesHealthApi/1.0"})
        with urlopen(request, timeout=ACTIVITY_TIMEOUT_SECONDS) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("live"), dict):
        return None
    return {"live": payload["live"], "engine": payload.get("engine") or {},
            "cache": payload.get("conversation_cache") or {},
            "totals": payload.get("totals") or {}}


SESSION_LOOKUP_TTL_SECONDS = 15


def _active_session() -> dict[str, Any]:
    """The conversation that most recently produced a message, across every profile.

    Uses message timestamps, not sessions.last_activity_at: the WebUI touches that column on UI
    polling, so it names whatever the browser last looked at rather than what the engine is
    serving. Cached for 15 s (the GPU sample runs every 2 s and this is one sqlite read per
    profile). Nothing is inferred from message text.
    """
    now = time.monotonic()
    cached = ACTIVITY_STATE.get("active_session")
    cached_at = ACTIVITY_STATE.get("active_session_at")
    if isinstance(cached, dict) and isinstance(cached_at, (int, float)) \
            and now - cached_at < SESSION_LOOKUP_TTL_SECONDS:
        return cached
    import glob
    import sqlite3
    cutoff = time.time() - 1800
    best = None
    dbs = [("default", "/home/clara/.hermes/state.db")]
    for path in sorted(glob.glob("/home/clara/.hermes/profiles/*/state.db")):
        dbs.append((path.split("/profiles/")[1].split("/")[0], path))
    for profile, db in dbs:
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                row = con.execute(
                    """select coalesce(m.session_id, ''), max(m.timestamp), substr(coalesce(s.title,''),1,80),
                              coalesce(s.message_count, 0)
                       from messages m left join sessions s on s.id = m.session_id
                       where m.timestamp > ?
                       group by m.session_id order by 2 desc limit 1""",
                    (cutoff,)).fetchone()
            finally:
                con.close()
        except Exception:
            continue
        if not row or not row[0]:
            continue
        if best is None or (row[1] or 0) > best[3]:
            best = (row[0], row[2] or "", profile, row[1] or 0, row[3])
    if best is None:
        result = {"status": "idle", "title": None, "session_id": None, "profile": None,
                  "age_seconds": None, "message_count": None}
    else:
        result = {"status": "active", "title": best[1], "session_id": best[0], "profile": best[2],
                  "age_seconds": round(time.time() - best[3], 1), "message_count": best[4],
                  "last_activity": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(best[3]))}
    ACTIVITY_STATE["active_session"], ACTIVITY_STATE["active_session_at"] = result, now
    return result


TOKEN_CACHE_TTL_SECONDS = 60


RATE_CACHE_TTL_SECONDS = 30
RATE_LINE_RE = re.compile(
    r"prompt (\d+) tokens = .*in (\d+) ms \(([0-9.]+) tok/s\), (\d+) generated in (\d+) ms \(([0-9.]+) tok/s\)")


def _rate_stats() -> dict[str, Any]:
    """Average and median tok/s: the boot totals give a true average, the log gives the median.

    A mean hides the shape here — one 80k prompt read and twenty one-token turns average into a number
    that describes neither. The median over the last ~200 completed requests is the number an operator
    recognises, and both are reported side by side. Cached 30 s; the engine log is on this host.
    """
    now = time.monotonic()
    cached = ACTIVITY_STATE.get("rate_stats")
    cached_at = ACTIVITY_STATE.get("rate_stats_at")
    if isinstance(cached, dict) and isinstance(cached_at, (int, float)) \
            and now - cached_at < RATE_CACHE_TTL_SECONDS:
        return cached
    out: dict[str, Any] = {"avg": {}, "median": {}, "samples": 0, "source": "engine log + /metrics totals"}
    # Averages from the engine's own cumulative counters (boot-scoped, which is what "today" is here).
    try:
        totals = (_strata_live() or {}).get("totals") or {}
    except Exception:
        totals = {}
    prompt_ms = totals.get("prompt_ms") or 0
    decode_ms = totals.get("decode_ms") or 0
    if prompt_ms:
        # prompt_tokens counts reused tokens too, while prompt_ms times only the tokens actually read:
        # dividing the total by it reports a four-figure "average prefill" that never happened.
        fresh = max(0, (totals.get("prompt_tokens") or 0) - (totals.get("reused") or 0))
        out["avg"]["prefill_tok_s"] = round(fresh / (prompt_ms / 1000.0), 1)
    if decode_ms:
        out["avg"]["decode_tok_s"] = round((totals.get("output_tokens") or 0) / (decode_ms / 1000.0), 1)
    if totals.get("prompt_tokens"):
        out["avg"]["reuse_pct"] = round(100.0 * (totals.get("reused") or 0) / totals["prompt_tokens"], 1)
    # Medians from the last completed request lines.
    pre, dec = [], []
    try:
        with open(STRATA_ENGINE_LOG, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 3_000_000))
            chunk = fh.read().decode("utf-8", "replace")
        for line in chunk.splitlines():
            if "strata serve: prompt " not in line:
                continue
            m = RATE_LINE_RE.search(line)
            if not m:
                continue
            try:
                pre.append(float(m.group(3)))
                gen = int(m.group(4))
                dec_rate = float(m.group(6))
            except (TypeError, ValueError):
                continue
            if gen > 1 and dec_rate > 0:
                dec.append(dec_rate)
        pre = pre[-200:]
        dec = dec[-200:]
    except OSError:
        pass
    import statistics
    if pre:
        out["median"]["prefill_tok_s"] = round(statistics.median(pre), 1)
    if dec:
        out["median"]["decode_tok_s"] = round(statistics.median(dec), 1)
    out["samples"] = len(pre)
    out["today"] = _today_rates()
    out["updated_at"] = iso_utc()
    ACTIVITY_STATE["rate_stats"], ACTIVITY_STATE["rate_stats_at"] = out, now
    return out


ENGINE_TODAY_PATH = Path.home() / ".hermes" / "state" / "engine-today.json"
_TODAY_LOCK = threading.Lock()


def _today_rates() -> dict[str, Any]:
    """Accumulate /metrics totals deltas into a bucket that resets at local midnight.

    The engine's own averages are boot-scoped: an engine that started yesterday 15:16 makes
    "average today" a 20-hour figure, and a cumulative decode mean is dragged down by the many
    1-token tool-call turns (measured 2026-11-01: cumulative 25 tok/s vs median 83). This bucket
    sums real deltas between health samples instead, and persists to engine-today.json so neither
    a service restart nor an engine restart loses the day.

    Baseline discipline: the persisted file carries the totals of the last sample booked. A
    missing file starts from a fresh baseline (never books a delta from zero, which would count
    the whole boot). An engine restart — totals.since changes, or any counter goes backwards —
    re-seeds the baseline and books nothing for that sample, so the bucket never goes negative.
    """
    now_local = datetime.now()
    today = now_local.strftime("%Y-%m-%d")
    midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        totals = (_strata_live() or {}).get("totals") or {}
    except Exception:  # noqa: BLE001 - a dashboard panel must not break on this
        totals = {}
    with _TODAY_LOCK:
        bucket: dict[str, Any] | None = None
        baseline: dict[str, Any] | None = None
        try:
            stored = json.loads(ENGINE_TODAY_PATH.read_text())
            if isinstance(stored, dict) and isinstance(stored.get("bucket"), dict) \
                    and stored.get("date") == today:
                bucket = stored["bucket"]
            if isinstance(stored, dict) and isinstance(stored.get("baseline"), dict):
                baseline = stored["baseline"]
        except (OSError, ValueError):
            pass
        if bucket is None:
            bucket = {"date": today, "output": 0, "prompt": 0, "reused": 0,
                      "decode_ms": 0.0, "prompt_ms": 0.0, "requests": 0}
        since = totals.get("since")
        if totals and baseline is not None:
            old_since = baseline.get("since")
            restart = (since is not None and old_since is not None and since != old_since) \
                or any(float(totals.get(k) or 0) < float(baseline.get(k) or 0) for k in
                       ("output_tokens", "prompt_tokens", "reused", "decode_ms", "prompt_ms",
                        "requests"))
            if not restart:
                delta_output = max(0.0, float(totals.get("output_tokens") or 0)
                                   - float(baseline.get("output_tokens") or 0))
                delta_prompt = max(0.0, float(totals.get("prompt_tokens") or 0)
                                   - float(baseline.get("prompt_tokens") or 0))
                delta_reused = max(0.0, float(totals.get("reused") or 0)
                                   - float(baseline.get("reused") or 0))
                delta_decode = max(0.0, float(totals.get("decode_ms") or 0)
                                   - float(baseline.get("decode_ms") or 0))
                delta_prompt_ms = max(0.0, float(totals.get("prompt_ms") or 0)
                                      - float(baseline.get("prompt_ms") or 0))
                delta_requests = max(0, int(totals.get("requests") or 0)
                                     - int(baseline.get("requests") or 0))
                if delta_output or delta_prompt or delta_decode or delta_prompt_ms or delta_requests:
                    bucket["output"] = int(bucket.get("output", 0)) + int(delta_output)
                    bucket["prompt"] = int(bucket.get("prompt", 0)) + int(delta_prompt)
                    bucket["reused"] = int(bucket.get("reused", 0)) + int(delta_reused)
                    bucket["decode_ms"] = float(bucket.get("decode_ms", 0.0)) + delta_decode
                    bucket["prompt_ms"] = float(bucket.get("prompt_ms", 0.0)) + delta_prompt_ms
                    bucket["requests"] = int(bucket.get("requests", 0)) + delta_requests
                    # kanban t_0c4cd846: the same delta books the 24-hour buckets, so the
                    # hourly graph reads this baseline without counting anything twice.
                    try:
                        _book_hourly_bucket(now_local, delta_output, delta_prompt, delta_reused,
                                            delta_decode, delta_prompt_ms, delta_requests)
                    except Exception:  # noqa: BLE001 - the hourly file must not break the today bucket
                        pass
        if totals:
            baseline = {k: totals.get(k) for k in
                        ("since", "requests", "prompt_tokens", "reused", "output_tokens",
                         "prompt_ms", "decode_ms")}
        try:
            ENGINE_TODAY_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = ENGINE_TODAY_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"date": today, "bucket": bucket, "baseline": baseline,
                                       "updated_at": iso_utc()}))
            os.replace(tmp, ENGINE_TODAY_PATH)
        except OSError:
            pass
    out: dict[str, Any] = {"date": today, "since": midnight.isoformat()}
    if not bucket.get("requests") and not bucket.get("output"):
        out["samples"] = 0
        return out
    out["samples"] = int(bucket.get("requests") or 0)
    out["output_tokens"] = int(bucket.get("output") or 0)
    out["prompt_tokens"] = int(bucket.get("prompt") or 0)
    out["reused"] = int(bucket.get("reused") or 0)
    decode_ms = float(bucket.get("decode_ms") or 0.0)
    prompt_ms = float(bucket.get("prompt_ms") or 0.0)
    if decode_ms:
        out["decode_tok_s"] = round(out["output_tokens"] / (decode_ms / 1000.0), 1)
    if prompt_ms:
        # prompt counts include reused tokens; prompt_ms times only the tokens actually read.
        fresh = max(0, out["prompt_tokens"] - out["reused"])
        out["prefill_tok_s"] = round(fresh / (prompt_ms / 1000.0), 1)
    if out["prompt_tokens"]:
        out["reuse_pct"] = round(100.0 * out["reused"] / out["prompt_tokens"], 1)
    return out


ENGINE_HOURLY_PATH = Path.home() / ".hermes" / "state" / "engine-hourly.json"
HOURLY_RETENTION_HOURS = 24


def _book_hourly_bucket(now_local: datetime, output: float, prompt: float, reused: float,
                        decode_ms: float, prompt_ms: float, requests: int) -> None:
    """Book one /metrics totals delta into a local-hour bucket (kanban t_0c4cd846).

    Called from _today_rates with the same delta, the same baseline and the same restart
    discipline as the 'today' bucket, so the two files can never double-count each other.
    Buckets are keyed "YYYY-MM-DDTHH", pruned to the newest 24 on every write, and
    persisted to engine-hourly.json so a day of graph survives a health-API restart.
    """
    key = now_local.strftime("%Y-%m-%dT%H")
    try:
        stored = json.loads(ENGINE_HOURLY_PATH.read_text())
        buckets = stored.get("buckets") if isinstance(stored, dict) else None
    except (OSError, ValueError):
        buckets = None
    if not isinstance(buckets, dict):
        buckets = {}
    entry = buckets.get(key)
    if not isinstance(entry, dict):
        entry = {"hour": key, "decode_ms": 0.0, "prefill_ms": 0.0, "output": 0,
                 "prompt": 0, "reused": 0, "requests": 0}
        buckets[key] = entry
    entry["output"] = int(entry.get("output", 0)) + int(output)
    entry["prompt"] = int(entry.get("prompt", 0)) + int(prompt)
    entry["reused"] = int(entry.get("reused", 0)) + int(reused)
    entry["decode_ms"] = float(entry.get("decode_ms", 0.0)) + decode_ms
    entry["prefill_ms"] = float(entry.get("prefill_ms", 0.0)) + prompt_ms
    entry["requests"] = int(entry.get("requests", 0)) + requests
    cutoff = (now_local - timedelta(hours=HOURLY_RETENTION_HOURS - 1)).strftime("%Y-%m-%dT%H")
    buckets = {k: v for k, v in buckets.items() if isinstance(k, str) and k >= cutoff}
    ENGINE_HOURLY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = ENGINE_HOURLY_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"buckets": buckets, "updated_at": iso_utc()}))
    os.replace(tmp, ENGINE_HOURLY_PATH)


def _hourly_history() -> list[dict[str, Any]]:
    """The last 24 hourly buckets with tok/s computed, oldest first (kanban t_0c4cd846).

    decode_tok_s = output / (decode_ms/1000); prefill_tok_s = (prompt - reused) /
    (prefill_ms/1000) — prompt counts include reused tokens, prompt_ms times only the
    tokens actually read; served_tok_s = (prompt + output) / ((prefill_ms + decode_ms)/1000).
    Hours with nothing booked are absent from the list; the SPA draws them as gaps.
    """
    try:
        stored = json.loads(ENGINE_HOURLY_PATH.read_text())
    except (OSError, ValueError):
        return []
    raw = stored.get("buckets") if isinstance(stored, dict) else None
    if not isinstance(raw, dict):
        return []
    cutoff = (datetime.now() - timedelta(hours=HOURLY_RETENTION_HOURS - 1)).strftime("%Y-%m-%dT%H")
    out: list[dict[str, Any]] = []
    for key in sorted(k for k in raw if isinstance(k, str) and k >= cutoff)[-HOURLY_RETENTION_HOURS:]:
        b = raw[key]
        if not isinstance(b, dict):
            continue
        decode_ms = float(b.get("decode_ms") or 0.0)
        prefill_ms = float(b.get("prefill_ms") or 0.0)
        output = int(b.get("output") or 0)
        prompt = int(b.get("prompt") or 0)
        reused = int(b.get("reused") or 0)
        entry: dict[str, Any] = {"hour": key, "output": output, "prompt": prompt,
                                 "reused": reused,
                                 "requests": int(b.get("requests") or 0),
                                 "decode_ms": round(decode_ms, 1), "prefill_ms": round(prefill_ms, 1)}
        if decode_ms > 0:
            entry["decode_tok_s"] = round(output / (decode_ms / 1000.0), 1)
        if prefill_ms > 0:
            entry["prefill_tok_s"] = round(max(0, prompt - reused) / (prefill_ms / 1000.0), 1)
        if decode_ms or prefill_ms:
            entry["served_tok_s"] = round((prompt + output)
                                          / ((prefill_ms + decode_ms) / 1000.0), 1)
        out.append(entry)
    return out


def _served_totals(totals: dict[str, Any]) -> dict[str, Any]:
    """What the engine has actually served, straight from its own cumulative counters (kanban t_85c9ce7b).

    `prompt_tokens` counts tokens the parked cache handed over as well as tokens that had to be read
    again, and `prompt_ms` times only the tokens actually read — so the served rate is
    (prompt + output) over (prompt_ms + decode_ms). Cache hits are not free time and are not
    subtracted here. A counter the engine does not report stays null; nothing is inferred.
    """
    totals = totals or {}
    prompt = totals.get("prompt_tokens")
    reused = totals.get("reused")
    output = totals.get("output_tokens")
    block: dict[str, Any] = {
        "prompt_tokens_total": prompt,
        "fresh_tokens": (prompt - reused) if isinstance(prompt, int) and isinstance(reused, int) else None,
        "reused_tokens": reused,
        "output_tokens": output,
        "requests": totals.get("requests"),
        "served_tok_s": None,
        "source": "engine /metrics totals (since engine start)",
        "updated_at": iso_utc(),
    }
    span = (float(totals.get("prompt_ms") or 0.0) + float(totals.get("decode_ms") or 0.0)) / 1000.0
    served = sum(v for v in (prompt, output) if isinstance(v, (int, float)))
    if span > 0 and served:
        block["served_tok_s"] = round(served / span, 1)
    return block


def _strata_key() -> str:
    """The engine's bearer key, from the same file the rest of the stack reads."""
    try:
        return open("/home/clara/.config/opencode/strata.key").read().strip()
    except OSError:
        return ""


def _token_usage() -> dict[str, Any]:
    """Token in/out over today, 7 days, 30 days and all time, summed from every profile's sessions.

    The engine's own /metrics totals answer "since this boot"; these windows answer "what has this box
    actually done", which is what a cost/quality judgement needs. Sums `sessions.input_tokens` /
    `output_tokens` (cumulative per session) by activity date, so a session spanning midnight lands in
    the window it was last active. Cached for 60 s: a sqlite scan per profile is not a 2 s job.
    """
    now = time.monotonic()
    cached = ACTIVITY_STATE.get("token_usage")
    cached_at = ACTIVITY_STATE.get("token_usage_at")
    if isinstance(cached, dict) and isinstance(cached_at, (int, float)) \
            and now - cached_at < TOKEN_CACHE_TTL_SECONDS:
        return cached
    import glob
    import sqlite3
    midnight = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    windows = {"today": midnight, "7d": time.time() - 7 * 86400, "30d": time.time() - 30 * 86400,
               "all": 0}
    totals = {k: {"in": 0, "out": 0, "cache_read": 0, "sessions": 0} for k in windows}
    dbs = ["/home/clara/.hermes/state.db"] + sorted(glob.glob("/home/clara/.hermes/profiles/*/state.db"))
    for db in dbs:
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                for name, cutoff in windows.items():
                    row = con.execute(
                        """select coalesce(sum(input_tokens),0), coalesce(sum(output_tokens),0),
                                  coalesce(sum(cache_read_tokens),0), count(*)
                           from sessions
                           where coalesce(last_activity_at, started_at) >= ?
                             and source is not null and source != 'oneshot'""",
                        (cutoff,)).fetchone()
                    totals[name]["in"] += row[0] or 0
                    totals[name]["out"] += row[1] or 0
                    totals[name]["cache_read"] += row[2] or 0
                    totals[name]["sessions"] += row[3] or 0
            finally:
                con.close()
        except Exception:
            continue
    for name, t in totals.items():
        # `input_tokens` is the uncached prompt side; cache_read_tokens rides beside it, so the share
        # served from cache is read/(read+in) — dividing by `in` alone gives values over 100%.
        total_prompt = t["in"] + t["cache_read"]
        t["prompt_total"] = total_prompt
        t["cache_pct"] = round(100.0 * t["cache_read"] / total_prompt, 1) if total_prompt else None
    engine = {}
    try:
        data = _strata_live()
        tot = (data or {}).get("engine") or {}
    except Exception:
        tot = {}
    result = {"windows": totals, "source": "state.db sessions (all profiles)",
              "updated_at": iso_utc()}
    ACTIVITY_STATE["token_usage"], ACTIVITY_STATE["token_usage_at"] = result, now
    return result


def _strata_prompt_cache(cache: dict[str, Any]) -> dict[str, Any]:
    """The parked-conversation cache, from the engine's own counters.

    The old path parsed llama.cpp's "cache state:" line out of `docker logs`, which this host no
    longer has, so the dashboard sat on "Prompt cache (RAM): unknown" forever. Strata reports the
    budget, the parked count, the parked bytes and the token counters in /metrics, so both sides of
    the cache — what came from RAM and what had to be read again — are read back instead of inferred.
    A counter the engine does not report stays null.
    """
    if not cache:
        return {"status": "unavailable", "prompts": None, "mib": None, "limit_mib": None,
                "tokens_limit": None, "tokens_est": None,
                "source": "engine /metrics conversation_cache", "updated_at": iso_utc()}
    parked_bytes = cache.get("bytes")
    budget = cache.get("budget_mib")
    parked_mib = round(parked_bytes / (1024 * 1024), 1) if isinstance(parked_bytes, (int, float)) else None
    prompt_tokens = cache.get("prompt_tokens")
    reused = cache.get("reused_tokens")
    token_pair = isinstance(prompt_tokens, (int, float)) and isinstance(reused, (int, float))
    slots, parked = cache.get("slots"), cache.get("parked")
    slot_pair = isinstance(slots, int) and isinstance(parked, int)
    return {
        "status": "healthy",
        "prompts": parked,
        "parked_slots": parked,
        "slots": slots,
        "free_slots": (slots - parked) if slot_pair else None,
        "mib": parked_mib,
        "parked_mib": parked_mib,
        "limit_mib": float(budget) if isinstance(budget, (int, float)) and budget > 0 else None,
        "budget_mib": budget,
        "free_mib": (round(budget - parked_mib, 1)
                     if isinstance(budget, (int, float)) and parked_mib is not None else None),
        "prompt_tokens": prompt_tokens,
        "reused_tokens": reused,
        "fresh_tokens": int(prompt_tokens - reused) if token_pair else None,
        "reuse_pct": (round(100.0 * reused / prompt_tokens, 1) if token_pair and prompt_tokens else None),
        "requests": cache.get("requests"),
        "requests_reused": cache.get("requests_reused"),
        "parks": cache.get("parks"),
        "restores": cache.get("restores"),
        "evictions": cache.get("evictions"),
        "tokens_limit": None,
        "tokens_est": None,
        "park_gain": cache.get("park_gain"),
        "min_free_mib": cache.get("min_free_mib"),
        "source": "engine /metrics conversation_cache",
        "updated_at": iso_utc(),
    }


def _expert_occupancy(engine: dict[str, Any]) -> dict[str, Any]:
    """Expert-cache occupancy on the pinned host arena: resident, secondary tier, free.

    The engine sizes the arena itself (arena_mib) and reports how much the expert cache holds
    (expert_cache_mib), how much of that sits in the primary tier (expert_cache_primary_mib) and the
    slot counts. Free space is arena minus resident — a subtraction of two reported numbers.
    """
    if not engine:
        return {"status": "unavailable"}
    arena = engine.get("arena_mib")
    resident = engine.get("expert_cache_mib")
    primary = engine.get("expert_cache_primary_mib")
    slots_total = engine.get("expert_slots")
    slots_primary = engine.get("expert_slots_primary")
    if not isinstance(arena, (int, float)) or not isinstance(resident, (int, float)):
        return {"status": "unavailable"}
    free = round(arena - resident, 1)
    return {
        "status": "healthy",
        "arena_mib": arena,
        "resident_mib": resident,
        "primary_mib": primary,
        "secondary_mib": (round(resident - primary, 1) if isinstance(primary, (int, float)) else None),
        "free_mib": free,
        "resident_pct": round(100.0 * resident / arena, 1) if arena else None,
        "free_pct": round(100.0 * free / arena, 1) if arena else None,
        "slots_total": slots_total,
        "slots_primary": slots_primary,
        "slots_free": (slots_total - slots_primary) if isinstance(slots_total, int)
        and isinstance(slots_primary, int) else None,
        "source": "engine /metrics engine block",
    }


def _strata_clients() -> list[dict[str, Any]]:
    """Which processes currently hold a connection to the engine, and how many sockets each.

    The engine cannot name its callers, so read the kernel's socket table instead. One entry per
    process: chat, a kanban worker and a subagent inside that worker are all separate requests, so
    this answers "who is asking", which is what the dashboard's client row is for. Cached for 2 s —
    an ss call per poll is waste.
    """
    cached = ACTIVITY_STATE.get("strata_clients")
    cached_at = ACTIVITY_STATE.get("strata_clients_at")
    now = time.monotonic()
    if isinstance(cached, list) and isinstance(cached_at, (int, float)) and now - cached_at < 2:
        return cached
    clients: list[dict[str, Any]] = []
    try:
        out = subprocess.run(["ss", "-tnp", "state", "established", "( dport = :8080 )"],
                             capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return cached if isinstance(cached, list) else []
    counts: dict[int, int] = {}
    for line in out.splitlines()[1:]:
        match = re.search(r"pid=(\d+)", line)
        if match:
            pid = int(match.group(1))
            counts[pid] = counts.get(pid, 0) + 1
    for pid, sockets in sorted(counts.items(), key=lambda item: -item[1]):
        name = ""
        # Prefer the systemd unit (hermes-dashboard.service, strata-priority-gate.service, a kanban
        # worker's scope): "python3" alone cannot tell a chat client from a subagent's worker.
        try:
            for line in open(f"/proc/{pid}/cgroup").read().splitlines():
                unit = line.rsplit("/", 1)[-1]
                if unit.endswith(".service") or unit.endswith(".scope"):
                    name = unit
                    break
        except OSError:
            pass
        if not name:
            try:
                argv0 = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")[0].decode(errors="replace")
                name = os.path.basename(argv0)
            except OSError:
                pass
        clients.append({"pid": pid, "name": name or f"pid {pid}", "sockets": sockets})
    ACTIVITY_STATE["strata_clients"] = clients
    ACTIVITY_STATE["strata_clients_at"] = now
    return clients


def _strata_activity() -> dict[str, Any] | None:
    """What the engine is doing, built from its own counters."""
    data = _strata_live()
    if data is None:
        return None
    live, engine = data["live"], data["engine"]
    state = (live.get("state") or "idle").lower()
    running = live.get("running") or 0
    rate = live.get("tok_s")
    slots_raw = live.get("slots") if isinstance(live.get("slots"), list) else []
    slot_count = live.get("parallel") or len(slots_raw) or (engine.get("batch_slots") or 1)

    prompt_progress = None
    generation = None
    if state == "reading":
        total = live.get("prompt_total") or 0
        read = live.get("prompt_read") or 0
        percent = round(read / total * 100, 1) if total else None
        eta = round((total - read) / rate, 1) if (rate and total) else None
        # The engine reports tok_s only for generation, so derive the prefill rate from the
        # change since the last sample; otherwise the panel shows a stale cumulative figure.
        now_m = time.monotonic()
        prev = ACTIVITY_STATE.get("strata_prefill") or {}
        if prev.get("total") == total and prev.get("processed") is not None:
            dt = now_m - (prev.get("at") or now_m)
            delta = read - prev["processed"]
            if dt > 1.0 and delta > 0:
                inst = delta / dt
                prev_rate = prev.get("rate")
                rate = round(0.6 * inst + 0.4 * prev_rate, 1) if prev_rate else round(inst, 1)
                if not (20 <= rate <= 1500):      # a slot copy or a resumed read jumps the counter
                    rate = None
                eta = round((total - read) / rate, 1) if rate else None
        ACTIVITY_STATE["strata_prefill"] = {"total": total, "processed": read, "at": now_m, "rate": rate}
        prompt_progress = {"total": total, "processed": read, "cached": 0,
                           "percent": percent, "rate_tokens_per_second": rate,
                           "eta_seconds": eta, "slot_id": None, "task_id": None}
        phase = "prefilling"
    elif state == "generating":
        done = live.get("generated") or 0
        cap = live.get("max_tokens") or 0
        # Hermes sends no max_tokens, so the engine invents one (222,878 seen live). A bar
        # drawn against that reads 0.6% forever and looks like a missing bar. Only trust a
        # cap a client could plausibly have asked for.
        if cap <= 0 or cap > 65536:
            cap = 0
        prompt_progress = None
        now_m = time.monotonic()
        prev_g = ACTIVITY_STATE.get("strata_gen") or {}
        if prev_g.get("max_tokens") == (cap or None) and prev_g.get("generated") is not None:
            dt = now_m - (prev_g.get("at") or now_m)
            delta = done - prev_g["generated"]
            if dt > 1.0 and delta > 0:
                inst = delta / dt
                prev_rate = prev_g.get("rate")
                rate = round(0.6 * inst + 0.4 * prev_rate, 1) if prev_rate else round(inst, 1)
        ACTIVITY_STATE["strata_gen"] = {"generated": done, "max_tokens": cap or None, "at": now_m, "rate": rate}
        generation = {"generated_tokens": done, "max_tokens": cap or None,
                      "decode_tokens_per_second": rate,
                      "percent": round(done / cap * 100, 1) if cap else None,
                      "slot_id": None, "task_id": None}
        phase = "generating"
    else:
        phase = "idle"

    # A bar only belongs to a slot we can identify: with one busy slot the global
    # progress is that slot's; with two, the engine reports a single live figure and
    # attributing it to either would be a guess.
    busy_slots = [s for s in slots_raw if (s.get("state") or "idle") != "idle"]
    slots = []
    for s in slots_raw:
        sid = s.get("slot")
        busy = (s.get("state") or "idle") != "idle"
        percent = None
        if busy and len(busy_slots) == 1:
            percent = (prompt_progress or {}).get("percent") or (generation or {}).get("percent")
        # Owner, 2026-11-05: the engine's per-slot tok_s is an instant sample -- a full MTP verify
        # window lands in one sample and reads as ~300 tok/s, then 60 the next. Keep the raw value
        # (peak) and publish a ~5 s exponential average, which is the number an operator recognises.
        _raw = s.get("tok_s")
        _avg = None
        if isinstance(_raw, (int, float)) and _raw >= 0:
            _ema = ACTIVITY_STATE.setdefault("slot_rateema", {})
            _ema_at = ACTIVITY_STATE.setdefault("slot_rateema_at", {})
            _now = time.monotonic()
            _prev = _ema.get(sid)
            _dt = _now - _ema_at.get(sid, _now)
            _a = min(0.5, max(0.05, _dt / 5.0)) if _dt > 0 else 0.2
            _avg = _raw if _prev is None else _prev + _a * (_raw - _prev)
            _ema[sid] = _avg
            _ema_at[sid] = _now
        slots.append({"id": sid, "n_ctx": engine.get("max_context"),
                      "is_processing": busy, "phase": s.get("state") or "idle",
                      "percent": percent,
                      "tok_s": s.get("tok_s"),
                      "tok_s_avg": round(_avg, 1) if isinstance(_avg, (int, float)) else None,
                      "generated": s.get("generated"),
                      "prompt_tokens": s.get("prompt_tokens") or s.get("held_tokens"),
                      "prompt_total": (prompt_progress or {}).get("total"),
                      "prompt_processed": (prompt_progress or {}).get("processed")})
    if not slots:
        slots = [{"id": i, "n_ctx": engine.get("max_context"), "is_processing": False,
                  "phase": "idle", "percent": None} for i in range(int(slot_count))]
    # The engine reads a prompt before it pins it to a slot, so during prefill every slot
    # reports idle while the engine is plainly busy. Show the real progress on one card,
    # flagged as engine-wide, instead of leaving the panel looking dead.
    if running and not busy_slots and slots:
        global_percent = (prompt_progress or {}).get("percent")
        if global_percent is None:
            global_percent = (generation or {}).get("percent")
        slots[0].update(is_processing=True, phase=state, percent=global_percent,
                        attributed="engine-wide")

    decoding = [s for s in slots if (s.get("phase") == "decoding")]
    reading = [s for s in slots if (s.get("phase") == "reading")]
    if decoding and reading:
        phase = "mixed"
    elif decoding:
        phase = "generating"
    elif reading:
        phase = "prefilling"
    # a slot's own decode rate is the honest figure while another slot reads
    if decoding:
        rates = [s.get("tok_s") for s in decoding if s.get("tok_s")]
        if rates:
            generation = {"generated_tokens": max(s.get("generated") or 0 for s in decoding),
                          "max_tokens": None, "decode_tokens_per_second": round(sum(rates), 1),
                          "percent": None, "slot_id": decoding[0].get("id"), "task_id": None}
    hindsight = _hindsight_queue() if "_hindsight_queue" in globals() else {"status": "unavailable"}
    queued = (live.get("queued") or 0) + (live.get("waiting") or 0)

    if phase == "generating":
        summary = (f"generating {generation['generated_tokens']:,} tokens"
                   + (f" at {rate} tok/s" if rate else ""))
    elif phase == "mixed":
        summary = (f"decoding {generation['generated_tokens']:,} tokens at "
                   f"{generation['decode_tokens_per_second']} tok/s while a prompt is read"
                   + (f" · {int(queued)} waiting" if queued else ""))
    elif phase == "prefilling":
        summary = (f"prefilling {prompt_progress['processed']:,}/{prompt_progress['total']:,} tokens"
                   f" ({prompt_progress['percent']}%)" + (f" at {rate} tok/s" if rate else ""))
    else:
        summary = "Idle · no active request"
    if queued:
        summary += f" · {int(queued)} waiting"

    # "running" counts every in-flight request (slots plus anything admitted outside them),
    # so reporting it as slot occupancy produced "5 / 2 slots busy".  Count the slots that
    # are actually working, and expose the in-flight number separately.
    busy_count = sum(1 for s in slots if s.get("is_processing"))
    # Owner, 2026-11-05: the engine never says which conversation sits in which slot, so the only
    # honest attribution is "there is exactly one busy slot, therefore it is the active session".
    # Stamping the same title on every busy slot made the dashboard show one task in all of them.
    _session_now = _active_session()
    _busy = [s for s in slots if s.get("is_processing")]
    if _session_now.get("session_id") and len(_busy) == 1:
        _busy[0]["session_title"] = _session_now.get("title")
        _busy[0]["session_id"] = _session_now.get("session_id")
    elif _busy:
        for _slot in _busy:
            _slot["attributed"] = "engine-wide"
    return {
        "status": "busy" if running else "idle",
        "phase": phase,
        # The engine's own words, unedited, so the panel can name a phase this layer folded away
        # (a slot that is `decoding` while another is `reading` is reported as "mixed" here).
        "engine_state": state,
        "engine_slot_states": sorted({str(s.get("phase")) for s in slots if s.get("phase")}),
        "slot_count": slot_count,
        "slots_processing": min(busy_count, int(slot_count)) if busy_count else min(int(running), int(slot_count)),
        "in_flight": running,
        "slots": slots,
        "prompt_progress": prompt_progress,
        "generation": generation,
        "last_prefill": None,
        "server": _llama_server_config(),
        "vram_budget": None,
        "queue": {"processing": int(running), "deferred": int(queued)},
        "clients": _strata_clients(),
        "prompt_cache": _strata_prompt_cache(data.get("cache") or {}),
        "expert_cache": _expert_occupancy(engine),
        "tokens": _token_usage(),
        "rates": _rate_stats(),
        # kanban t_85c9ce7b: the served block, from the engine's own cumulative counters.
        "served": _served_totals(data.get("totals") or {}),
        # kanban t_0c4cd846: 24 hourly buckets for the day-graph card. Persisted locally, so they
        # stay honest even while the engine is down.
        "history": {"hourly": _hourly_history()},
        "session": _session_now,
        "hindsight_operation": None,
        "hindsight": hindsight,
        "summary": summary,
        "source": "strata-live",
        # When this block was read. The panel compares it with its own fetch age: > 6 s old is
        # "stale", an unreachable engine is "engine down", and neither may be shown as a live phase.
        "sampled_at": iso_utc(),
        "sampled_at_epoch": int(time.time()),
        "stale_after_seconds": 6,
    }


def inference_activity_details() -> dict[str, Any]:
    """What the local model is doing: slot occupancy, throughput, clients, queue."""
    activity: dict[str, Any] = {
        "status": "unavailable",
        "phase": "unavailable",
        "slot_count": None,
        "slots_processing": None,
        "slots": [],
        "prompt_progress": None,
        "generation": None,
        "last_prefill": None,
        "server": None,
        "vram_budget": None,
        "queue": {"processing": None, "deferred": None},
        "clients": [],
        "prompt_cache": {"status": "unavailable"},
        # The served block has to exist even when the engine is down, or the panel would show a
        # rate against a missing engine. The hourly buckets stay honest either way.
        "served": {"status": "unavailable"},
        # Hindsight has its own service. It is queried even when the engine is down, because
        # "engine down" and "Hindsight down" are different facts and the panel must show both.
        "bank": HINDSIGHT_BANK,
        "hindsight": _hindsight_queue(),
        "summary": "Inference activity unavailable",
        "error": None,
        # Hourly buckets are persisted locally: they stay honest even while the engine is down.
        "history": {"hourly": _hourly_history()},
    }
    runtime = get_runtime_config()
    if runtime.get("status") != "healthy":
        activity["error"] = runtime.get("error", "Configured inference runtime is unavailable")
        return activity
    # Strata answers on this path: its own live block carries the progress that the
    # llama.cpp-shaped /slots payload does not.
    strata_activity = _strata_activity()
    if strata_activity is not None:
        return strata_activity
    try:
        request = Request(
            runtime["base_url"] + "/slots",
            headers={**authorization_headers(runtime), "User-Agent": "HermesHealthApi/1.0"},
        )
        with urlopen(request, timeout=ACTIVITY_TIMEOUT_SECONDS) as response:
            raw_slots = json.load(response)
    except HTTPError as exc:
        if exc.code in {401, 403}:
            activity["error"] = f"Configured inference runtime rejected health authorization (HTTP {exc.code})"
        else:
            activity["error"] = f"Configured inference runtime returned HTTP {exc.code}"
        exc.close()
        return activity
    except (URLError, TimeoutError, OSError):
        activity["error"] = "Configured inference runtime could not be reached"
        return activity
    except (ValueError, TypeError, json.JSONDecodeError):
        activity["error"] = "Configured inference runtime returned invalid slot data"
        return activity
    if not isinstance(raw_slots, list):
        activity["error"] = "Configured inference runtime returned an unexpected slot payload"
        return activity
    slots = [
        {"id": slot.get("id"), "n_ctx": slot.get("n_ctx"),
         "is_processing": bool(slot.get("is_processing")),
         "task_id": slot.get("id_task"),
         "prompt_tokens": slot.get("n_prompt_tokens"),
         "prompt_tokens_processed": slot.get("n_prompt_tokens_processed"),
         "prompt_tokens_cached": slot.get("n_prompt_tokens_cache")}
        for slot in raw_slots if isinstance(slot, dict)
    ]
    processing = sum(1 for slot in slots if slot["is_processing"])
    active_task = next((slot["task_id"] for slot in slots if slot["is_processing"]), None)
    active_slot = next((slot for slot in slots if slot["is_processing"]), None)
    try:
        counters = _llama_metrics()
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError, json.JSONDecodeError):
        counters = {}
    # Publish generation progress only when it belongs to the task running right now,
    # otherwise a finished task's final line would read as live activity.
    generation = _live_generation()
    if generation is not None and (active_task is None or generation["task_id"] != active_task):
        generation = None
    # Per-slot phase and prompt progress: with two slots both can be busy at once, so each
    # card must report its own state instead of inheriting the headline slot's.
    now_monotonic = time.monotonic()
    for slot in slots:
        slot_total = slot.get("prompt_tokens") or 0
        slot_processed = slot.get("prompt_tokens_processed") or 0
        slot["prompt_total"] = slot_total
        slot["prompt_processed"] = slot_processed
        if not slot["is_processing"]:
            slot["phase"], slot["percent"] = "idle", None
        elif generation is not None and generation.get("task_id") == slot.get("task_id"):
            slot["phase"], slot["percent"] = "generating", None
        elif slot_total and slot_processed < slot_total:
            slot["phase"] = "prefilling"
            slot["percent"] = round(slot_processed / slot_total * 100, 1)
        else:
            slot["phase"], slot["percent"] = "processing", None
    prompt_progress = None
    if active_slot is not None and active_slot.get("phase") == "prefilling":
        prompt_total = active_slot.get("prompt_total") or 0
        prompt_processed = active_slot.get("prompt_processed") or 0
        # Live prefill rate from the previous sample of the same task, so the panel can show
        # real progress and an ETA instead of just a percentage that appears to stall.
        previous_prefill = ACTIVITY_STATE.get("prefill") or {}
        rate = None
        # Only a sample of the SAME prompt may be used for the rate. llama.cpp reports the
        # growing prompt of the next request in the same slot, and comparing across two
        # different prompts produced impossible rates (measured >2000 t/s against a real
        # ceiling near 830). The total also drifts by a few tokens as the prompt is tokenised,
        # so allow a small tolerance rather than demanding an exact match.
        previous_total = previous_prefill.get("total")
        similar_prompt = isinstance(previous_total, int) and abs(prompt_total - previous_total) <= max(64, 0.02 * previous_total)
        if previous_prefill.get("task_id") == active_slot.get("task_id") \
                and similar_prompt \
                and isinstance(previous_prefill.get("processed"), int):
            elapsed = now_monotonic - float(previous_prefill.get("at") or now_monotonic)
            delta = prompt_processed - previous_prefill["processed"]
            if elapsed > 0 and delta > 0:
                candidate = round(delta / elapsed, 1)
                # A sample taken as the prompt finishes crawls (measured 1.5 t/s) and would
                # publish a nonsense ETA, so ignore rates too low to be steady-state prefill.
                if candidate >= 20:
                    rate = candidate
        ACTIVITY_STATE["prefill"] = {"task_id": active_slot.get("task_id"),
                                     "total": prompt_total,
                                     "processed": prompt_processed, "at": now_monotonic}
        prompt_progress = {
            "total": prompt_total,
            "processed": prompt_processed,
            "cached": active_slot.get("prompt_tokens_cached") or 0,
            "percent": active_slot.get("percent"),
            "rate_tokens_per_second": rate,
            "eta_seconds": round((prompt_total - prompt_processed) / rate, 1) if rate else None,
            "task_id": active_slot.get("task_id"),
            "slot_id": active_slot.get("id"),
        }
    else:
        ACTIVITY_STATE["prefill"] = {}
    if generation is not None:
        phase = "generating"
    elif processing and prompt_progress is not None:
        phase = "prefilling"
    elif processing:
        phase = "processing"
    else:
        phase = "idle"
    clients = _slot_clients()
    hindsight = _hindsight_queue()
    client_details = _client_labels(clients)
    session_info = _active_session_title() if processing else {"title": None}
    hindsight_ops = _hindsight_operation_label()
    def _client_label(entry: dict[str, Any]) -> str:
        resolved = client_details.get(str(entry.get("pid"))) or {}
        return resolved.get("label") or entry.get("name") or f"pid {entry.get('pid')}"
    who = ", ".join(sorted({_client_label(entry) for entry in clients})) or "no connected client"
    # Per-slot attribution: which connected client is driving each slot. With the
    # single-client case this is unambiguous; with several clients we can report the
    # set of clients but not a per-slot pairing, so the label degrades to that set.
    client_names = [_client_label(entry) for entry in clients]
    single_client = client_names[0] if len(client_names) == 1 else None
    hindsight_active = bool((hindsight or {}).get("processing") or (hindsight or {}).get("pending"))
    active_session_title = session_info.get("title") if isinstance(session_info, dict) else None
    # Slot pinning is a llama.cpp feature.  Hindsight used to send id_slot through
    # HINDSIGHT_API_LLM_EXTRA_BODY, but it no longer does (/etc/hermes-hindsight.env has no
    # EXTRA_BODY) and Strata does not accept id_slot at all: it assigns slots itself.  So there
    # is no pin, and claiming one would label a client's slot as Hindsight's.  Empty default =
    # report the set of clients instead of inventing a pid->slot pairing.
    _pinned_raw = (os.environ.get("CLARA_HINDSIGHT_SLOT") or "").strip()
    pinned_slot = int(_pinned_raw) if _pinned_raw.isdigit() else None
    op_label = " · ".join(part for part in (
        (hindsight_ops or {}).get("operation"),
        (hindsight_ops or {}).get("scope"),
    ) if part)
    for slot in slots:
        if not slot["is_processing"]:
            slot["client"] = slot["session_title"] = slot["hindsight_operation"] = None
            continue
        if pinned_slot is not None and slot.get("id") == pinned_slot and hindsight_active:
            slot["client"] = "Hindsight memory"
            slot["session_title"] = None
            slot["hindsight_operation"] = op_label or "unknown"
        else:
            slot["client"] = single_client or (", ".join(client_names) if client_names else None)
            slot["session_title"] = active_session_title or "unknown"
            slot["hindsight_operation"] = None
    deferred = counters.get("requests_deferred")
    if phase == "generating":
        summary = (f"{who} is generating {generation['generated_tokens']:,} tokens at "
                   f"{generation['decode_tokens_per_second']} tok/s")
    elif phase == "mixed":
        summary = (f"decoding {generation['generated_tokens']:,} tokens at "
                   f"{generation['decode_tokens_per_second']} tok/s while a prompt is read"
                   + (f" · {int(queued)} waiting" if queued else ""))
    elif phase == "prefilling":
        summary = (f"{who} is prefilling {prompt_progress['processed']:,}/"
                   f"{prompt_progress['total']:,} prompt tokens ({prompt_progress['percent']}%)")
        if prompt_progress.get("rate_tokens_per_second"):
            summary += f" at {prompt_progress['rate_tokens_per_second']} tok/s"
            if prompt_progress.get("eta_seconds") is not None:
                summary += f", ~{prompt_progress['eta_seconds']}s left"
    elif phase == "processing":
        summary = f"{who} holds the slot; phase not reported yet"
    else:
        summary = "Idle · no active request"
    if isinstance(deferred, float) and deferred > 0:
        summary += f" · {int(deferred)} request(s) queued"
    activity.update(
        status="busy" if processing else "idle",
        phase=phase,
        slot_count=len(slots),
        slots_processing=processing,
        slots=slots,
        prompt_progress=prompt_progress,
        generation=generation,
        last_prefill=_last_prefill_rate(),
        # Tuning context: what the server was told to do, and where the VRAM actually went.
        server=_llama_server_config(),
        vram_budget=_vram_budget(),
        queue={
            "processing": int(counters["requests_processing"]) if "requests_processing" in counters else None,
            "deferred": int(deferred) if isinstance(deferred, float) else None,
        },
        clients=clients,
        # Host RAM held by the prompt cache, from the engine's own "cache state" line.
        prompt_cache=_prompt_cache_state(),
        session=session_info if isinstance(session_info, dict) else {"title": None},
        hindsight_operation=hindsight_ops,
        hindsight=hindsight,
        summary=summary,
    )
    return activity


def _epoch_file(path: Path, max_age_seconds: int) -> dict[str, Any]:
    try:
        value = float(path.read_text(encoding="utf-8-sig").strip())
        updated = datetime.fromtimestamp(value, timezone.utc)
        age = max(0.0, (utc_now() - updated).total_seconds())
        return {
            "status": "healthy" if age <= max_age_seconds else "stale",
            "updated_at": iso_utc(updated),
            "age_seconds": round(age, 1),
            "max_age_seconds": max_age_seconds,
        }
    except (OSError, UnicodeError, ValueError):
        return {"status": "missing", "age_seconds": None, "max_age_seconds": max_age_seconds}


def _cron_script_enabled(script_name: str) -> bool:
    payload = read_json(HERMES_ROOT / "cron" / "jobs.json")
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
    return any(
        isinstance(job, dict)
        and job.get("enabled")
        and job.get("script") == script_name
        for job in jobs
    )


def _optional_heartbeat(path: Path, max_age_seconds: int, script_name: str) -> dict[str, Any]:
    if not _cron_script_enabled(script_name):
        return {
            "status": "disabled",
            "reason": f"cron script {script_name} is paused or absent",
            "age_seconds": None,
            "max_age_seconds": max_age_seconds,
        }
    return _epoch_file(path, max_age_seconds)


def audit_details() -> dict[str, Any]:
    audit_root = Path("/mnt/d/audit") if os.name != "nt" else Path(r"D:\audit")
    return {
        "audit_daemon": _optional_heartbeat(
            audit_root / "heartbeat.txt", 300, "audit_watchdog.py"
        ),
        "tapo_audio_recorder": _optional_heartbeat(
            audit_root / "audio" / "record_heartbeat.txt", 300, "record_watchdog.py"
        ),
    }


def wsl_keepalive_details() -> dict[str, Any]:
    for process in psutil.process_iter(["pid", "cmdline", "create_time"]):
        try:
            command = process.info.get("cmdline") or []
            if command[:2] == ["/bin/sleep", "infinity"]:
                return {
                    "status": "healthy",
                    "pid": process.info["pid"],
                    "started_at": iso_utc(
                        datetime.fromtimestamp(float(process.info["create_time"]), timezone.utc)
                    ),
                }
        except (psutil.Error, OSError, TypeError, ValueError):
            continue
    return {"status": "unhealthy", "reason": "Windows WSL keepalive client is absent"}


def cron_details() -> dict[str, Any]:
    root = HERMES_ROOT / "cron"
    payload = read_json(root / "jobs.json")
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
    if not isinstance(jobs, list):
        jobs = []
    enabled = [job for job in jobs if isinstance(job, dict) and job.get("enabled")]
    failing = [job for job in enabled if str(job.get("last_status") or "").lower() in {"error", "failed", "failure"}]

    key_jobs: list[dict[str, Any]] = []
    for job in enabled:
        search = " ".join(
            str(job.get(key) or "") for key in ("name", "prompt", "script")
        ).lower()
        categories = []
        if any(token in search for token in ("tapo", "camera", "audio-record", "audio-transcribe", "audit-daemon")):
            categories.append("tapo/audit")
        if "line" in search or "gateway" in search:
            categories.append("line")
        if not categories and job not in failing:
            continue
        key_jobs.append(
            {
                "id": job.get("id"),
                "name": job.get("name"),
                "categories": categories or ["failing"],
                "schedule": job.get("schedule_display"),
                "state": job.get("state"),
                "last_run_at": job.get("last_run_at"),
                "next_run_at": job.get("next_run_at"),
                "last_status": job.get("last_status") or "pending",
                "last_error": str(job.get("last_error") or "")[:400] or None,
            }
        )

    scheduler = _epoch_file(root / "ticker_heartbeat", 180)
    return {
        "scheduler": scheduler,
        "summary": {
            "configured": len(jobs),
            "enabled": len(enabled),
            "failing": len(failing),
            "healthy": max(0, len(enabled) - len(failing)),
        },
        "key_jobs": key_jobs,
    }


def file_state(path: Path) -> dict[str, Any]:
    payload = read_json(path)
    try:
        modified = iso_utc(datetime.fromtimestamp(path.stat().st_mtime, timezone.utc))
    except OSError:
        modified = None
    return {"updated_at": modified, "state": payload}



def usage_attribution() -> dict:
    """Who is consuming the box: top RSS consumers, VRAM split, and engine throughput totals.

    Answers "what is using that" without guessing: every number is read from /proc, nvidia-smi,
    or the engine's own /metrics totals. Never raises - a dashboard panel must not break the feed.
    """
    out: dict = {"memory": {}, "vram": {}, "engine": {}}
    try:
        mem = psutil.virtual_memory()
        used = mem.total - mem.available
        rows = []
        for proc in psutil.process_iter(["name", "memory_info"]):
            try:
                rss = proc.info["memory_info"].rss
            except Exception:
                continue
            if rss:
                rows.append((rss, proc.info["name"] or "?"))
        rows.sort(reverse=True)
        merged: dict = {}
        for rss, name in rows:
            merged[name] = merged.get(name, 0) + rss
        top = [{"name": n, "rss_mb": round(b / 1048576, 1),
                "pct_of_used": round(100.0 * b / used, 1) if used else 0.0}
               for b, n in sorted(((v, k) for k, v in merged.items()), reverse=True)[:6]]
        out["memory"] = {"total_mb": round(mem.total / 1048576, 1),
                         "used_mb": round(used / 1048576, 1),
                         "available_mb": round(mem.available / 1048576, 1),
                         "pinned_arena_mib": None, "top": top}
    except Exception as exc:  # noqa: BLE001
        out["memory"] = {"error": str(exc)}
    try:
        key = open(Path.home() / ".config/opencode/strata.key").read().strip()
        req = Request("http://127.0.0.1:8080/metrics",
                      headers={"Authorization": "Bearer " + key})
        with urlopen(req, timeout=6) as resp:
            m = json.load(resp)
        e = m.get("engine") or {}
        t = m.get("totals") or {}
        pm = float(t.get("prompt_ms") or 0) / 1000.0
        dm = float(t.get("decode_ms") or 0) / 1000.0
        offered = float(t.get("drafts_offered") or 0)
        out["vram"] = {"expert_cache_mib": e.get("expert_cache_mib"),
                       "expert_slots": e.get("expert_slots"),
                       "vram_free_mib": e.get("vram_free_mib"),
                       "arena_mib": e.get("arena_mib"),
                       "batch_slots": e.get("batch_slots") or e.get("parallel") or 1,
                       "kv": e.get("kv")}
        out["engine"] = {
            "requests": t.get("requests"),
            "prompt_tokens": t.get("prompt_tokens"),
            "output_tokens": t.get("output_tokens"),
            "prefill_tok_s": round(float(t.get("prompt_tokens") or 0) / pm, 1) if pm else None,
            "decode_tok_s": round(float(t.get("output_tokens") or 0) / dm, 1) if dm else None,
            "draft_accept_pct": round(100.0 * float(t.get("drafts_accepted") or 0) / offered, 1) if offered else None,
            "cache_reuse_pct": round(100.0 * float(t.get("reused") or 0) / float(t.get("prompt_tokens") or 1), 1),
        }
    except Exception as exc:  # noqa: BLE001
        out["engine"] = {"error": str(exc)}
    return out


def build_snapshot() -> dict[str, Any]:
    sampled_at = utc_now()
    service_results: dict[str, Any] = {}
    alerts: list[str] = []

    for name, definition in SERVICES.items():
        status_code, latency_ms, error = request_status(definition["url"])
        healthy = status_code in definition["expected"]
        result: dict[str, Any] = {
            "status": "healthy" if healthy else "unhealthy",
            "http_status": status_code,
            "latency_ms": latency_ms,
            "url": definition["url"],
            "critical": bool(definition["critical"]),
        }
        if error:
            result["error"] = error
        heartbeat_path = definition.get("heartbeat")
        if heartbeat_path:
            heartbeat = heartbeat_details(heartbeat_path)
            result["heartbeat"] = heartbeat
            process_running = bool((heartbeat.get("process") or {}).get("running"))
            healthy = healthy and bool(heartbeat.get("fresh")) and process_running
            result["status"] = "healthy" if healthy else "unhealthy"
        if not healthy:
            alerts.append(f"{name} is unhealthy")
        service_results[name] = result

    tunnel = tunnel_details()
    if tunnel["status"] != "healthy":
        pass

    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    commit = windows_commit_details()
    gpu = nvidia_gpu_details()
    inference = llama_cpp_details()
    cron = cron_details()
    audit = audit_details()
    wsl_keepalive = wsl_keepalive_details()
    disks: dict[str, Any] = {}
    disk_paths = (("C", "C:\\"), ("D", "D:\\")) if os.name == "nt" else (("C", "/mnt/c"), ("D", "/mnt/d"))
    for drive_name, drive_path in disk_paths:
        try:
            usage = psutil.disk_usage(drive_path)
            disks[drive_name] = {
                "total_bytes": usage.total,
                "free_bytes": usage.free,
                "used_percent": usage.percent,
            }
        except OSError:
            continue

    if memory.available < 1 * 1024**3:
        alerts.append("available physical memory is below 1 GiB")
    if swap.free < 2 * 1024**3:
        alerts.append("free page file is below 2 GiB")
    commit_available = commit.get("available_bytes")
    if isinstance(commit_available, int) and commit_available < 8 * 1024**3:
        alerts.append(
            f"system commit headroom is low at {commit_available / 1024**3:.1f} GiB"
        )
    for drive, usage in disks.items():
        if usage["used_percent"] >= 92:
            alerts.append(f"{drive}: disk usage is critical at {usage['used_percent']:.1f}%")
        elif usage["used_percent"] >= 85:
            alerts.append(f"{drive}: disk usage is high at {usage['used_percent']:.1f}%")
    if gpu.get("status") != "healthy":
        alerts.append("NVIDIA GPU telemetry is unavailable")
    for device in gpu.get("devices", []):
        index = device.get("index", "?")
        used_percent = device.get("memory_used_percent")
        temperature = device.get("temperature_c")
        if isinstance(used_percent, (int, float)) and used_percent >= 98:
            alerts.append(f"GPU {index} VRAM usage is high at {used_percent:.1f}%")
        if isinstance(temperature, (int, float)):
            # 80 C is NORMAL for an RTX 3060 under a 200k-token prefill (the cards
            # report no throttle reason at all there), so alerting on temperature
            # alone made the whole host read "degraded" every time the summariser
            # ran. Alert when the card is actually being throttled, or when it is
            # hot enough to matter (85 C).
            throttling = device.get("thermal_slowdown")
            if temperature >= 85 or (temperature >= 80 and throttling is True):
                alerts.append(
                    f"GPU {index} temperature is high at {temperature:.0f} C"
                    + (" (thermal throttling active)" if throttling else "")
                )
    if inference.get("status") != "healthy":
        alerts.append("llama.cpp inference backend is unhealthy")
    if cron.get("summary", {}).get("failing"):
        alerts.append(f"{cron['summary']['failing']} enabled Hermes cron job(s) are failing")
    for name, heartbeat in audit.items():
        if heartbeat.get("status") not in {"healthy", "disabled"}:
            alerts.append(f"{name.replace('_', ' ')} heartbeat is {heartbeat.get('status')}")
    if wsl_keepalive.get("status") != "healthy":
        pass

    critical_failed = any(
        result["critical"] and result["status"] != "healthy"
        for result in service_results.values()
    )
    support_failed = any(result["status"] != "healthy" for result in service_results.values())
    if critical_failed:
        overall = "unhealthy"
    elif support_failed or alerts:
        overall = "degraded"
    else:
        overall = "healthy"

    state_db = HERMES_ROOT / "state.db"
    try:
        state_db_bytes = state_db.stat().st_size
    except OSError:
        state_db_bytes = None

    return {
        "schema_version": 1,
        "generated_at": iso_utc(sampled_at),
        "sample_interval_seconds": SAMPLE_INTERVAL_SECONDS,
        "host": socket.gethostname(),
        "overall_status": overall,
        "alerts": alerts,
        "services": service_results,
        "reverse_tunnel": tunnel,
        "system": {
            "booted_at": iso_utc(datetime.fromtimestamp(psutil.boot_time(), timezone.utc)),
            "cpu_percent": psutil.cpu_percent(interval=None),
            "memory": {
                "total_bytes": memory.total,
                "available_bytes": memory.available,
                "used_percent": memory.percent,
            },
            "page_file": {
                "total_bytes": swap.total,
                "used_bytes": swap.used,
                "free_bytes": swap.free,
                "used_percent": swap.percent,
            },
            "commit": commit,
            "disks": disks,
        },
        "inference": inference,
        "hindsight_control": control_state(),
        "gpu": gpu,
        "cron": cron,
        "audit": audit,
        "ollama": {"status": "retired", "loaded_models": []},
        "storage": {"hermes_state_db_bytes": state_db_bytes},
        "usage_attribution": usage_attribution(),
        "watchdogs": {
            "wsl_keepalive": wsl_keepalive,
            "systemd_recovery": {
                "status": "healthy",
                "policy": "Restart=always; readiness is monitored asynchronously",
            },
            "legacy_windows_stack": {
                "status": "retired",
                "reason": "Hermes runtime migrated to WSL2 systemd",
            },
            "legacy_windows_gateways": {
                "status": "retired",
                "reason": "Gateway supervision migrated to WSL2 systemd",
            },
        },
    }


class SnapshotCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot: dict[str, Any] = {
            "schema_version": 1,
            "generated_at": iso_utc(),
            "overall_status": "starting",
            "alerts": ["initial health sample is pending"],
        }

    def set(self, snapshot: dict[str, Any]) -> None:
        with self._lock:
            self._snapshot = snapshot

    def get(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot.copy()


CACHE = SnapshotCache()
STOP_EVENT = threading.Event()


def sampler() -> None:
    while not STOP_EVENT.is_set():
        try:
            CACHE.set(build_snapshot())
        except Exception:
            logger.exception("health sampling failed")
        STOP_EVENT.wait(SAMPLE_INTERVAL_SECONDS)


class GpuSnapshotCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sampled_monotonic = None
        self._snapshot = {"schema_version": 1, "status": "starting", "devices": [],
                          "sample_interval_seconds": GPU_SAMPLE_INTERVAL_SECONDS,
                          "generated_at": None, "error": "Initial GPU sample pending"}

    def set(self, snapshot: dict[str, Any]) -> None:
        with self._lock:
            self._snapshot = snapshot
            self._sampled_monotonic = time.monotonic()

    def get(self) -> dict[str, Any]:
        with self._lock:
            result = self._snapshot.copy()
            age = (time.monotonic() - self._sampled_monotonic
                   if self._sampled_monotonic is not None else None)
        result["age_seconds"] = round(age, 3) if age is not None else None
        result["stale"] = age is None or age > GPU_STALE_AFTER_SECONDS
        return result


GPU_CACHE = GpuSnapshotCache()
SLOT_HISTORY = collections.deque(maxlen=1800)
SLOT_HISTORY_LOCK = threading.Lock()


def _strata_live_snapshot() -> dict[str, Any] | None:
    """Live per-slot Strata state; None when the engine is unavailable."""
    runtime = get_runtime_config()
    if runtime.get("status") != "healthy":
        return None
    try:
        request = Request(runtime["base_url"] + "/metrics",
                          headers={**authorization_headers(runtime), "User-Agent": "HermesHealthApi/1.0"})
        with urlopen(request, timeout=2) as response:
            payload = json.load(response)
        live = payload.get("live") if isinstance(payload, dict) else None
        return live if isinstance(live, dict) else None
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _record_slot_history(live: dict[str, Any]) -> None:
    # Slot keys follow the engine's own slot list instead of a hardcoded 0..2: Strata runs
    # `parallel` batch slots (4 today), and a slot that is decoding but not recorded would
    # silently drop out of the dashboard's average decode rate.
    point = {"ts": int(time.time()), "prefill_tok_s": live.get("prefill_tok_s_mean")}
    for item in (live.get("slots") or []):
        if not isinstance(item, dict):
            continue
        slot = item.get("slot")
        if slot is None:
            continue
        point[f"slot{slot}_tok_s"] = item.get("tok_s")
    with SLOT_HISTORY_LOCK:
        SLOT_HISTORY.append(point)


def build_gpu_live_snapshot() -> dict[str, Any]:
    started = time.monotonic()
    sampled_at = iso_utc()
    base = {"schema_version": 1, "generated_at": sampled_at,
            "sample_interval_seconds": GPU_SAMPLE_INTERVAL_SECONDS,
            "source": "NVIDIA driver via native WSL nvidia-smi",
            "utilization_scope": "device-wide GPU kernel activity; not per-process CUDA utilization"}
    fields = ("index,name,memory.total,memory.used,memory.free,utilization.gpu,"
              "utilization.memory,temperature.gpu,power.draw,power.limit,fan.speed,"
              "clocks.current.sm,clocks.max.sm,pcie.link.gen.current,pcie.link.width.current,"
              "clocks_event_reasons.sw_thermal_slowdown,clocks_event_reasons.active")
    try:
        response = subprocess.run(
            [LIVE_NVIDIA_SMI, "--query-gpu=" + fields, "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=1.5, check=True,
        )
        # CSV handles GPU names containing commas without shifting numeric columns.
        import csv
        devices = []
        for row in csv.reader(response.stdout.splitlines(), skipinitialspace=True):
            if len(row) != 17:
                raise ValueError("Unexpected NVIDIA telemetry column count")
            values = [_number(value) for value in row[2:15]]
            (total, used, free, utilization, memory_utilization, temperature, power, power_limit,
             fan, sm_clock, sm_clock_max, pcie_gen, pcie_width) = values
            thermal = row[15].strip().lower()
            devices.append({
                "index": int(row[0]), "name": row[1].strip(),
                "memory_total_bytes": int(total * 1024 ** 2) if total is not None else None,
                "memory_used_bytes": int(used * 1024 ** 2) if used is not None else None,
                "memory_free_bytes": int(free * 1024 ** 2) if free is not None else None,
                "memory_used_percent": round(used / total * 100, 1) if used is not None and total else None,
                "utilization_percent": utilization, "temperature_c": temperature,
                "power_w": power, "power_limit_w": power_limit, "fan_percent": fan,
                # Tuning diagnostics: memory-controller load vs SM load, clock headroom, link
                # width (cross-GPU activation traffic for layer splits), and throttle reasons.
                "memory_utilization_percent": memory_utilization,
                "sm_clock_mhz": sm_clock, "sm_clock_max_mhz": sm_clock_max,
                "pcie_link_gen": pcie_gen, "pcie_link_width": pcie_width,
                "thermal_slowdown": True if thermal == "active" else False if thermal == "not active" else None,
                "throttle_reasons": _throttle_reasons(row[16] if len(row) > 16 else ""),
            })
        if not devices:
            raise ValueError("NVIDIA returned no GPUs")
        base.update(status="healthy", devices=devices, device_count=len(devices))
        for field in ("memory_total_bytes", "memory_used_bytes", "memory_free_bytes"):
            base[field] = (sum(device[field] for device in devices)
                           if all(device[field] is not None for device in devices) else None)
    except (OSError, subprocess.SubprocessError, ValueError):
        # Do not publish previous readings or fabricated zeroes as a fresh sample.
        base.update(status="unavailable", devices=[], error="NVIDIA telemetry query failed or timed out")
    # Inference activity rides the same 2 s sample: the dashboard needs "what is the
    # model doing" at the same cadence as the GPU readings it sits next to.
    try:
        base["inference_activity"] = inference_activity_details()
        activity = base.get("inference_activity")
        if isinstance(activity, dict):
            try:
                activity["request_capture"] = recent_capture_snapshot(12) if recent_capture_snapshot else {"status": "unavailable", "requests": []}
                try:
                    req = Request("http://127.0.0.1:8090/__health", headers={"User-Agent": "HermesHealthApi/1.0"})
                    with urlopen(req, timeout=0.75) as response:
                        admission = json.loads(response.read(16384)).get("admission_queue", {})
                    activity["admission_queue"] = {
                        "status": "healthy", "waiting": int(admission["waiting"]),
                        "capacity": int(admission["capacity"]),
                        "max_wait_seconds": float(admission["max_wait_seconds"]),
                        "discipline": str(admission["discipline"]),
                        "foreground_waiting": int(admission.get("foreground_waiting", 0)),
                        "hindsight_waiting": int(admission.get("hindsight_waiting", 0)),
                        "hindsight_active": int(admission.get("hindsight_active", 0)),
                        "daytime_hours_bangkok": str(admission.get("daytime_hours_bangkok", "unknown")),
                        "hindsight_daytime_shared_limit": int(admission.get("hindsight_daytime_shared_limit", 1)),
                        "hindsight_night_limit": int(admission.get("hindsight_night_limit", 1)),
                    }
                except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError, TypeError):
                    activity["admission_queue"] = {"status": "unavailable"}
            except Exception:  # noqa: BLE001 - capture must never block health telemetry
                activity["request_capture"] = {"status": "unavailable", "requests": []}
    except Exception:  # noqa: BLE001 - never blank the GPU sample for this
        logger.exception("inference activity sampling failed")
        base["inference_activity"] = {"status": "unavailable",
                                      "summary": "Inference activity sampling failed"}
    # Control state so the dashboard can render its Hindsight buttons honestly.
    try:
        base["hindsight_control"] = control_state()
    except Exception:  # noqa: BLE001
        base["hindsight_control"] = {"paused": None}
    try:
        live = _strata_live_snapshot()
        activity = base.get("inference_activity")
        if isinstance(activity, dict) and isinstance(live, dict):
            activity["prefill_tok_s_mean"] = live.get("prefill_tok_s_mean")
            by_id = {str(item.get("slot")): item for item in (live.get("slots") or []) if isinstance(item, dict)}
            for slot in activity.get("slots", []):
                slot["tok_s"] = by_id.get(str(slot.get("id")), {}).get("tok_s")
            _record_slot_history(live)
    except Exception:  # noqa: BLE001 - history must never break the live sample
        pass
    base["slot_history"] = list(SLOT_HISTORY)
    base["query_duration_ms"] = round((time.monotonic() - started) * 1000, 1)
    return base


def gpu_sampler() -> None:
    while not STOP_EVENT.is_set():
        started = time.monotonic()
        try:
            GPU_CACHE.set(build_gpu_live_snapshot())
        except Exception:
            GPU_CACHE.set({"schema_version": 1, "status": "unavailable", "devices": [],
                           "generated_at": iso_utc(), "sample_interval_seconds": 2,
                           "error": "GPU sampler failed"})
            logger.exception("GPU sampling failed")
        STOP_EVENT.wait(max(0, GPU_SAMPLE_INTERVAL_SECONDS - (time.monotonic() - started)))


class HealthHandler(BaseHTTPRequestHandler):
    server_version = "HermesHealthApi/1.0"

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Vary", "Accept")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if (self.path in {"/", "/health", "/v1/health"}
                and self.headers.get("Accept", "").strip() == "application/vnd.clara.gpu+json"):
            payload = GPU_CACHE.get()
            payload["slot_history"] = list(SLOT_HISTORY)
            self._send_json(200, payload)
            return
        if self.path in {"/", "/health", "/v1/health"}:
            payload = CACHE.get()
            payload["slot_history"] = list(SLOT_HISTORY)
            self._send_json(200, payload)
            return
        if self.path == "/v1/gpu":
            payload = GPU_CACHE.get()
            payload["slot_history"] = list(SLOT_HISTORY)
            self._send_json(200, payload)
            return
        if self.path == "/healthz":
            snapshot = CACHE.get()
            healthy = snapshot.get("overall_status") in {"healthy", "degraded"}
            self._send_json(
                200 if healthy else 503,
                {
                    "status": snapshot.get("overall_status", "unknown"),
                    "generated_at": snapshot.get("generated_at"),
                },
            )
            return
        if self.path == "/v1/schema":
            self._send_json(
                200,
                {
                    "schema_version": 1,
                    "health": "/v1/health",
                    "liveness": "/healthz",
                    "gpu": "/v1/gpu",
                    "gpu_refresh_seconds": GPU_SAMPLE_INTERVAL_SECONDS,
                    "refresh_seconds": SAMPLE_INTERVAL_SECONDS,
                    "transport": "Ubuntu loopback through reverse SSH",
                },
            )
            return
        self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        """Dashboard controls: pause / resume / digest-now.

        Reached through the nginx proxy, which maps the dashboard's API path onto "/" here and
        enforces the dashboard's basic auth, so access control lives there, not in this process.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except Exception:
            payload = {}
        action = str((payload or {}).get("action") or "").strip()
        if action == "pause":
            self._send_json(200, pause_hindsight((payload or {}).get("hours")))
            return
        if action == "resume":
            self._send_json(200, resume_hindsight())
            return
        if action == "digest-now":
            self._send_json(200, digest_now())
            return
        if action == "status":
            self._send_json(200, control_state())
            return
        self._send_json(400, {"error": "unknown_action", "action": action})

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("client=%s %s", self.client_address[0], fmt % args)


class HealthServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    CACHE.set(build_snapshot())
    worker = threading.Thread(target=sampler, name="health-sampler", daemon=True)
    worker.start()
    threading.Thread(target=gpu_sampler, name="gpu-sampler", daemon=True).start()
    server = HealthServer((HOST, PORT), HealthHandler)
    logger.info("health API listening on http://%s:%d", HOST, PORT)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        STOP_EVENT.set()
        server.server_close()


if __name__ == "__main__":
    main()
