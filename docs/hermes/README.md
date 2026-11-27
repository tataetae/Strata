# Hermes on clara-ai-server — state, decisions and tooling

Everything needed to rebuild this box's Hermes setup after a reset.
Written 2026-11-21, after the v0.23.3 merge and the GPU3 hardware failure.

## The branches on the fork (tataetae/Strata)

| branch | what it is |
|---|---|
| `clara/engine-0.1.56` | **the live engine's source of record**: the 0.1.56 tree, self-consistent (its C++ prefetch AND its serve layer), the tree the binary is built from |
| `clara/prefill-main` | the **live tree's serve layer** (the 0.1.52 checkout that runs on the box: the prompt-progress chunks, the queue view, the prefetch hint) |
| `clara/hermes-state` | **this directory**: the state, the record, the tooling |

**The box runs a mix**: the 0.1.56 C++ binary (`strata-src/engine/strata`) with the 0.1.52 tree's
Python serve layer (`strata-src/serve/server.py`). Both halves carry the 27 November changes; a
rebuild from `clara/engine-0.1.56` alone gives a consistent tree.

## What this directory holds

| file | what it is |
|---|---|
| `AGENTS.md` | the full session record (2026-08 → 2026-11-21): every measurement, the dead ends, the config values |
| `tools/` | the scripts written during the 2026-11-20/21 sessions (see below) |
| `strata-iq4xs-3gpu-live.json` | the **live** engine profile (3 GPUs, 128 cache slots, `--kv int8`) |
| `strata-iq4xs-4gpu.json` | the 4-GPU profile to restore once the dead card is reseated |
| `systemd/*.conf` | the drop-ins that put the services on the Python 3.14 runtime |
| `hermes-hindsight.env` | the Hindsight service env (the LLM model is now the live build) |

## The current state (2026-11-27 11:10 UTC)

| | |
|---|---|
| Hermes | **0.23.3** (sha `33cbdad2ef` + the progress/queue work), 18 profiles, api_server + hermex connected |
| Engine | Strata UD-IQ4_XS on **4 GPUs**, 11,211-11,329 expert slots, chunk 6,912, `--kv k8v4` |
| Conversation cache | `--conversation-cache-slots 512`, `--conversation-cache-mib 262144`, **evictions 0** |
| Memory | the engine's cgroup high 88 GiB (its floor is ~70), swap 271 GiB of NVMe |
| Throughput (quiet box) | a 32,832-token cold read **548 / 499 tok/s**, an 84,082-token read **536 tok/s**, decode **36.6 tok/s** |
| The bot chat's wait after a tool call | p50 **148-254 s** per turn; the engine had **5-111 requests queued** for its 2 slots |
| Health | `logs/health_check.sh` 22 pass / 0 fail - `logs/hermes_health_check.sh` 21 pass / 0 fail |
| Cron lanes | **19 jobs** (was 40), **one agent a day** |

## What changed since 2026-11-21 (the 27 November session)

### The wait after a tool call, and the four fixes

The bot chat's cycle is **LLM turn (p50 208 s) -> tool (p50 60 s) -> LLM turn**. The analysis and
the fixes are in `AGENTS.md` (the sections of 2026-11-27 09:20 -> 11:05). In short:

1. **The stale watchdog was never where it was configured to be.** `providers.strata...stale_timeout_seconds`
   never applied: the runtime provider id is `custom`, not `strata`, so the local branch took over
   with its **900 s** default - and **120 "Stream stale for 900s" kills in 3 days** (30 h thrown
   away) on 144k-336k-token contexts. **The knob that works is `agent.local_stream_stale_timeout`
   (now 2700 s, in every config)**, plus `HERMES_API_TIMEOUT=3600` in `~/.hermes/.env` and every
   profile's `.env` (the socket read timeout must not fire before the watchdog).
2. **The engine's prompt-progress chunks.** `prompt_progress()` returned `None` under `--batch`, so
   a client asking for `return_progress` got silence and the stall timer killed healthy prefills.
   It now answers under `--batch` too, carries **the queue**, and `/props` advertises
   `prompt_progress: true`. Hermes asks for it (a cached `/props` probe) and shows
   *"processing prompt - 42 % - 6 requests ahead"*.
3. **The prefetch.** The serve layer sends the waiting request's first 4 096 tokens as a `PRE`
   control line; the engine touches the pages of the matching parked entry, so the NVMe page-in
   (26-31 s when the snapshot was swapped out) overlaps the wait for a slot.
4. **The queue is visible.** The clamp proxy's `GET /queue` now carries the engine's state
   (the queue depth, the slots, the read progress, the shape): `tools/clamp/strata_clamp_proxy.py`.

### The crons: 40 -> 19, twelve agent workers -> one

The 40 schedules on a 2-slot engine were a permanent queue of workers. Deleted 22 (the dead
Windows/WSL paths, the passed one-shots, the obsolete, the two-month-stale, the cloud-era, the
seven phone spammers), folded three Hindsight consolidation jobs into one, and created **one**
agent lane (`nightly maintenance sweep`, 03:30) that does the stability watch, the backup
freshness and the Hindsight check **in order inside one turn**. Every delivery is now `local`.

### The 6-GPU plan

In `AGENTS.md` (the section of 2026-11-27 11:10): what a new board + 2 more RTX 3060s buy, what
the motherboard must give (the CPU's PCIe lanes, XMP, ASPM, the fixed Gen 4 link), what to change
in the profile, and how to measure it against the yardstick above.


## The five things that were wrong, and the fix

### 1. A stale model name leaked conversations to a cloud provider

`Compression summary failed: Error code: 404 - model "qwen3.8-flash-next-q4_k_xl" not found.
Try glm-5.3-flash:cloud.` — that error text is **ollama.com's**, so a transcript was being offered to a
cloud provider.

Cause: **571 session rows still carried deleted build names** (`qwen3.8-flash-next-iq3_s` × 545,
`q4_k_xl` × 25, `q3_xl` × 1), and the ROOT profile's `auxiliary.compression` still pointed at
`ollama-cloud / deepseek-v4.1-flash:1017`.

Fix: every stale session repointed at `qwen3.8-flash-next-iq4_xs`; every profile's
`auxiliary.compression` is now `strata / qwen3.8-flash-next-iq4_xs`; the one cloud-routed live session
pinned local. **849 session rows** in total were moved off cloud models.
`codex-worker` keeps `gpt-6.1-sol` / `openai-codex` — the owner's stated exception.

Tool: `tools/localize.py` (repoint), `tools/pin_local.py` (one session), `tools/cloud_audit.py` (report).

### 2. The conversation cache's "evictions" were the SLOT CEILING, not memory

`include/strata/core/conversation_cache.hpp:395`:

```cpp
while (!entries_.empty() && (entries_.size() >= slots_ || bytes() > budget_ - held - incoming))
    if (!evict_oldest()) return false;
```

The RAM gate would log *"evicted N parked conversations"* or *"skip parking (physical RAM admission)"* —
**neither appears anywhere in the log**. With `--conversation-cache-slots 16` and **1,439** distinct live
conversations competing, the 17th evicted the oldest: **107 evictions** and only a **60 % reuse rate**
(314k of 781k tokens re-read).

Fix: `--conversation-cache-slots 128`. The slot count is a **ceiling, not a RAM budget** — the RAM side is
the cgroup `memory.high` (80 GiB) and `--conversation-cache-mib 262144` (256 GiB logical), and the surplus
pages to the NVMe swap (`STRATA_PARK_SWAP_GIB=256`, 251 GiB free). Result: **evictions 0**.

### 3. The engine's layer split is coupled to the LIVE GPU count

`src/program/generate.cpp:3539-3560` derives `--split-device` from the number of **visible** cards and
requires `len(layer_split) == n_dev - 1`. When GPU3 fell off the bus, the profile's 3-value
`layer_split` (11,23,41) was rejected outright → exit 2 → a crash loop. The fix is `layer_split: "auto"`
(the engine's own search) and a matching `gpu` list.

### 4. GPU3 fell off the PCIe bus (still needs hardware)

```
18:28:19  NVRM: Xid (PCI:0000:08:00): 79, GPU has fallen off the bus.
```

That is the card behind the **M.2_2 riser / root port `00:1a.0`** — the AER errors on that port had been
escalating all evening (18:04:56, 18:15:49, 18:22:05 → the fatal). The owner was going to reseat it.

Recovered without a reboot: `rmmod`/`modprobe` of the nvidia modules brought CUDA back to 3 devices
(`nvidia_modeset` needed `-f`, `nvidia_drm` held it; Hindsight had to be stopped for the reload).
`nvidia-smi -r -i 3` **cannot** reset a card that has fallen off the bus. The remaining fix is physical:
reseat the riser, or reboot the box (the modules reload at boot), then restore `strata-iq4xs-4gpu.json`.

**Note on the surprise:** the engine ran fine on 4 GPUs *and* on 3 — the split is not what limits it.
4 GPUs gave 12,920 expert slots / chunk 8,192 and a 2.5 ms CPU-expert time per window; 3 GPUs give
7,244 slots / chunk 6,400 and 17–27 ms. The 3-GPU shape is roughly **half the decode throughput**
(7.6–13.7 tok/s vs 20–40), and that is the one place the missing card really costs.

### 5. v0.23.3 needs the Python 3.14 runtime

v0.23.3's pins are `python_version >= '3.14'`, but the repo `venv/` is **Python 3.11**, so it resolved to
pydantic 2.13.4 while the code imported `pydantic_core.ArgsKwargs` (3.14-only) — the gateway crash-looped
with `ImportError: cannot import name 'ArgsKwargs'`.

Fix: the services run through the tree launcher, which execs the store python (3.14.9):

```ini
# /etc/systemd/system/hermes-gateway.service.d/90-runtime-v231.conf
[Service]
ExecStart=
ExecStart=/home/clara/.hermes/hermes-agent/.hermes/bin/hermes gateway run --external-supervisor
```

and the same for the dashboard. Unit files backed up as `*.service.bak-20261121-v231`.

## The tools (all under `tools/`)

| tool | what it does |
|---|---|
| `fire_compress.py` + `run_compress.sh` | fires the summariser on one session **standalone** (builds a throwaway `AIAgent`, runs the shared `compress_now()` core, commits the rotation). `run_compress.sh` picks the 3.14 runtime. |
| `store.py` | version-compatible session-store access (the v0.23 persistence API vs the deprecated `SessionDB`; plain SQLite for reads) |
| `batch_run2.py` + `build_batch.py` | finds every live chat over its profile's threshold and compacts them, one at a time, skipping what is already done |
| `verify_all.py` | re-tokenises the chats with the model's own tokenizer to measure what the compaction actually saved |
| `cloud_audit.py` | reports every cloud model still reachable from a profile, an aux route or a session row |
| `localize.py` / `pin_local.py` | repoint sessions / one session at the local engine |
| `fix_models.py` | repoint the stale session model names |
| `fix_profiles.py` | give a profile a local `auxiliary.compression` route |
| `thai_check.py` / `thai_check_think.py` | the Thai leak probes (CJK/English fragments in the output) |
| `watchdog.sh` | stops the compaction batch if the engine stops being healthy. **It needs 3 CONSECUTIVE failed `/health` probes** — `/health` times out under load and a single timeout is not a failure. |
| `measure.py` / `soak.py` | the engine measurement harnesses (prefill / decode) |

### The v0.23.3 API moves the tools had to follow

- **`hermes_state.SessionDB` is deprecated** (removed in v0.27.0): no `_read_ctx`, no
  `update_session_model`. The new API is
  `persistence_assembly.open_persistence(profile_scope(home=...))` → `p.sessions.update_model(...)`.
  For a *read*, plain SQLite (`file:...?mode=ro`) still works and is version-independent.
- **The compression lock needs the persistence handle.** With neither `agent._session_db` nor a
  persistence handle, `compress_now()` returns `lock_skipped` ("the lock check failed") instead of
  compressing. Pass `persistence=` when the handle exists.
- **Stale locks accumulate** when a process dies mid-compression: `compression_locks` and
  `session_turn_leases` keep the dead PID and every later attempt is skipped. Clear the rows whose holder
  PID is gone or whose `expires_at` has passed.

## Rebuilding after a reset

1. **The engine** (if local inference is wanted again — the owner decided not to use it after
   2026-11-21): restore `strata-iq4xs-3gpu-live.json` (or the 4-GPU one after the riser is reseated) to
   `local-inference/strata-src/strata-iq4xs.json`, then `systemctl restart strata-iq3s-server`.
2. **Hermes**: the tree lives in `~/.hermes/hermes-agent`; the merge commit is `33cbdad2ef` on `develop`.
   Its own git remote is the upstream (`NousResearch/hermes-agent`, read-only) — **the branch was never
   pushed anywhere**, because the upstream denies write access to this account. The merge is preserved
   in this directory (`AGENTS.md` documents it commit by commit) but the *tree* needs a home of its own;
   a fork of `NousResearch/hermes-agent` under `tataetae` is the obvious place.
3. **The runtime drop-ins** (`systemd/*.conf`) — without them the gateway crash-loops on `ArgsKwargs`.
4. **Hindsight** (`hermes-hindsight.env`) — the LLM model must be the live build.
5. **Every profile's models** must point at the local engine (except `codex-worker`); `tools/cloud_audit.py`
   verifies it and `tools/localize.py` fixes the session rows.
6. **The health checks** are in `~/logs/` (`health_check.sh`, `hermes_health_check.sh`,
   `HEALTH-CHECKLIST.md`) plus the hourly timer `strata-engine-hourly.timer` / `engine_hourly.py`.
