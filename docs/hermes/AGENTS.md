# AGENTS.md — clara-ai-server (short form)

**Host** `clara-ai-server`, native Ubuntu 26.04.1, LAN `192.168.1.36`.
No Windows/WSL AI PC exists any more. Full notes: `workspace/archive/AGENTS-home-full-20261028.md`.
Superseded engine configs, rejected-by-measurement tunings, old measured baselines, and WebUI/Hindsight
quirks live in the **Hindsight bank** (tags `strata`, `engine-config`, `history`) — recall them, never
re-derive them.

## Storage
- Root LV 295 GiB · inference LV `/srv/inference` 629 GiB, ~487 GiB free. Swap 208 GB on NVMe
  (`vm.swappiness=100`, `vm.min_free_kbytes=512 MiB`, persisted in `/etc/sysctl.d/99-strata-parking.conf`).
- **Owner authorises a swap file up to ~400 GB on `/srv/inference`** — parked conversations and session
  snapshots reach disk instead of being evicted. This buys cache retention, not throughput.
- Old disks are **read-only**, not in fstab: `/mnt/hdd` (1 TB, **failing — replace**), `/mnt/wsl`, `/mnt/win`.

## RAM: tight is normal, paging is the design

**Do not treat low `MemAvailable` as a fault.** With the engine up this box runs at **0.5–2 GB available**
and that is expected: experts plus KV slots plus parked snapshots are meant to fill RAM. Paging is enabled
deliberately so cold pages (including parked conversations) go to NVMe instead of being destroyed. The
engine's parking floor is 256 MiB **on purpose**. RAM is 80 GB since 2026-11-04.

**Judge pressure by these, not by free RAM:**

| signal | read it with | alarm when |
|---|---|---|
| PSI memory stall | `grep -E '^(some\|full)' /proc/pressure/memory` | `avg10` above ~5 for minutes |
| swap churn | `/proc/vmstat` `pswpin`/`pswpout` | climbing fast while the engine is idle |
| engine pages on disk | `/proc/<engine>/status` → `VmSwap` | growing without parked conversations growing |
| restore cost | engine log `restored … in N ms` | above ~5 s routinely |
| parking refusals | engine log `skip parking` | reappearing after a config change |

**Never "fix" low memory by compacting context, killing services, or shrinking the parking budget.**
A slow turn on this box is almost always a queue or a prefill, not memory pressure.

## Inference — Strata
- `strata-iq3s-server` on `0.0.0.0:8080`, model **`qwen3.8-flash-next-iq4_xs`** since 2026-11-19 (profile
  `strata-iq4xs.json`; engine log confirms it loads `packs/iq4_xs` experts + the `UD-IQ4_XS` PLE table, KV `k8v4`, MTP on).
  The unit name still says `iq3s` and every profile's `pack=` field still points at `packs/q4_k_xl` — neither selects the
  quant; do not infer the served model from either. Rollback profiles kept: `strata-q4_kxl.json`, `strata-q3_xl.json`, `strata-iq3_s.json`.
  The served model name is **not validated** — `/v1/chat/completions` accepts any `model` string.
- **Q4 live shape (2026-11-18, 262,144 ctx, no YaRN): 3 slots / 3 groups**, `--batch-mtp` + `STRATA_PIPED_MTP=2`,
  `--vram-reserve-mib 1024`, `--layer-split 11,23,41` (config key `layer_split`, **not** an `args` entry),
  expert cache **19.4 GiB** (2,185 / 2,022 / 1,159 / 1,148 slots per card), conversation cache 16 GiB / 8 slots,
  chunk **6,144 tokens**, **`STRATA_ARENA_PIN_GIB=70`** (donate the whole Q4 arena; 46 slices, the engine caps it).
  Measured with a fixed 16k-token cold prompt: prefill **330 tok/s**; 3 clients × 600 tok → **20–27 tok/s aggregate**,
  ~9 per stream; MTP 55–67 % kept. 4 slots is worse on both (chunk 2,816 → prefill ~200–280, aggregate 17–24)
  because each slot's K/V comes straight out of the expert cache.
- **Overnight 2026-11-18 → 19 (driven by opencode session `ses_e7f4d5812ffeBQ0fRDkhea6Xd0`, not a person).**
  All verified on the box, not taken from its self-report:
  - `STRATA_ARENA_PIN_GIB` swept 32 → **0** → **70**. At `0` the engine cannot start at all:
    `mtp: the K/V state does not fit (111–469 MiB of 11911 MiB VRAM free)`, ~22–32 s crash loop, 62+ restarts.
    **`70` is the working floor for MTP at 262k ctx; `0` is not a usable value.**
  - `--parallel` swept 4 → 3 as an ExecStart override in `experiment.conf`. The profile JSON still says
    `parallel = 1`, so **read ExecStart, not the JSON**, for real parallelism.
  - New: `/etc/sysctl.d/99-strata-hugepages.conf` → `vm.nr_hugepages = 36800` (71.9 GiB; only 75 pages free —
    the arena holds them) and `STRATA_ARENA_RAM_FALLBACK=0`.
  - **Hardware watchdog armed:** `/etc/modules-load.d/strata-watchdog.conf` (`iTCO_wdt` → `/dev/watchdog0`) plus
    `/etc/systemd/system.conf` `RuntimeWatchdogSec=30s` and `RebootWatchdogSec=2min` (backup
    `system.conf.bak-20261118-1718`). A systemd stall now hard-resets the box in 30 s. Owner decision pending;
    reversal = drop those two lines + `daemon-reexec`.
  - Deliberately stopped by that session: `hermes-hindsight` (reads as `failed`, exit 3 **from its SIGTERM — not a
    crash**) and `strata-priority-gate`. Both are to come back when the sweep ends. **Do not troubleshoot
    hindsight as a fault while it is intentionally held down.**
  - Load chain, so stopping one thing does not stop the load: gateway plugin → `strata-priority-gate:8082` →
    `strata_clamp_proxy` (`:8081`) → `:8080`. The proxy keeps an established socket, so the engine stays busy for
    minutes after the gate is stopped.
  - Sweep state when paused: live split `11,23,41`; arm A `11,23,36` and arms B/C **not run**. Finishing needs
    three more engine restarts — owner-timed.
- **Measured throughput (independent single-stream probe, system-maintainer, 11-18 ~19:15–20:30 Bangkok):**
  prefill cold **246 tok/s @ 6.3k** and **319 tok/s @ 18.8k**; decode **15.6–20 tok/s** (natural traffic p50 14.7,
  max 23.3; log peaks 26.7–28.0). A cached repeat of the *same* prompt returns TTFT **0.20–0.39 s**
  (10.6k–16.1k tok/s effective) but only on **3 of 6** identical repeats — parking is per slot, so a repeat that
  lands on another slot re-prefills cold. **So: a fresh 50k-token worker prompt costs ~160–210 s; warm, under 1 s.**
  My earlier concurrency figures are void — the sweep stopped the engine mid-test (12:51:39Z, `BrokenPipeError`
  12:51:45) and three engine generations ran inside one measurement window.
- **What bounds it:** DRAM measured **34.9 GB/s** aggregate (sysbench, 256 MiB blocks to defeat the 18 MB L3,
  2133 MT/s; theoretical dual-channel 34.1) while the GPUs draw only **43–79 W of a 170 W limit**. The Q4 expert
  set streams from RAM, so decode is memory-bound, not compute-bound. Discard any 1 MiB-block sysbench figure —
  those read 52–185 GB/s and are L3-resident junk.
- **Why Q4 is ~2–3× slower than IQ3, measured on the same box and harness (2026-11-18):**

  | shape | cache | chunk | prefill | aggregate decode (4×600) |
  |---|---|---|---|---|
  | IQ3_XXS, 4 slots | 23.0 GiB / 14.8k slots | 6,912 | **540** | **68.8** |
  | Q4, 4 slots | 15.7 GiB / 5.3k slots | 2,816 | ~250 | 23.6 |
  | Q4, 3 slots | 19.4 GiB / 6.5k slots | 6,144 | 330 | 20–27 |

  The mechanism is in `strata decode timing`: Q4 runs **16–19 expert swaps per window** and 5–12 CPU-computed
  expert entries/window (CPU 33–94 ms/window), against IQ3's ~2 entries and 10 ms. The 72 GiB Q4 expert set only
  fits ~27 % in VRAM, so the adaptive tier thrashes. **RAM pinning cannot fix this** — pin 40 → 70 bought only
  ~15 % (285 → 330 tok/s); the constraint is VRAM, not RAM. `strata-vision` also holds **1,160 MiB**; dropping
  `--vision` buys ~380 slots if images are not needed.
- **512k / YaRN is parked, not solved.** The MTP draft K/V is per slot and full-context: at 524,288 it does not fit
  on any card (4 slots or 3). Dropping `--mtp`/`--batch-mtp` frees the VRAM and loads at 512k, but piped drafting
  **and** conversation parking both stop (a batch slot's conversation is overwritten un-parked) — worse than the
  context is worth. If retried: `--rope-scaling yarn --rope-scale 2` works, but never append a bare `yarn` token
  to the args (engine exits code 2, "unknown argument: yarn").
- **Prompt-chunk trap:** the auto chunk sizer caps at what the *poorest* stage can lend (85 % of its expert-cache
  slots, keeping 128). At `11,23,37` Q4 left CUDA3 348 slots → chunk **512** → prefill ~50 tok/s. Rebalancing to
  `11,23,41` gave 861/721 slots → chunk 3,328. Watch for `WARNING: prompt chunk N tokens, not 8192` in the log.
  `--prefill-head-alias` is unavailable on multi-GPU ("supports one GPU only").
- **Q4 start traps:** the MTP gguf must live outside the shard directory (any extra `.gguf` there breaks shard
  metadata); `--ple-gguf` must point at the **last** shard (00004); the cgroup cap must be ≥ ~100 GiB
  (`MemoryLow 72G / High 100G / Max 104G`) or the arena pin dies mid-load with no OOM event.
- **IQ3 config (2026-11-07, measured sweep — historical, superseded by the Q4 shape above): FOUR batch slots, FOUR groups, piped MTP on, full-precision draft head.**
  `--parallel 4` (drop-in ExecStart), `--batch-groups 4`, `--batch-mtp`, `--mtp-q4` absent (full head),
  `--lookup-chain 4`, `--vram-reserve-mib 512` (Q4: 1024), `--kv k8v4`, `--prompt-cache 8` + spread,
  `--pipeline-windows 2`, `--layer-split 11,23,37` (Q4: 11,23,41) + `--trim-stage-weights`, `--spec 8`, `--spec-min-p 0.5`,
  `--max-context 262144`, `--ple-row-cache 262144`, `--adapt-min-gain 2.5`, `--expert-cache auto`,
  conversation cache **16,384 MiB / 8 slots** / min-free 256 / park-gain 2000, `--session-save-reclaim`.
  Env: `arena.conf` has `STRATA_PIPED_MTP=2` (2 proposals per slot per window), `STRATA_PIPED_CONCURRENT=2`
  (**was 1 through the IQ3 era; it is 2 now** — see the overnight note below),
  `STRATA_ADMIT_SHORT_FIRST=2048`, `STRATA_PW_WITH_BATCH=1`, `STRATA_ROUTE_TAIL_SKIP=0`, `STRATA_DECODE_TIMING=1`;
  `experiment.conf` has `STRATA_PARK_SWAP_GIB=256`, `CPUAffinity=0-9`, `CPUWeight=1000`.
  Measured (4 concurrent clients, 10-min soak): expert cache **19,389 MiB**, VRAM 46.7/48, aggregate
  **139.4 tok/s** (p90 178.4, peak 196.8), **~25 tok/s per stream**, 149 completions, 0 faults.
  Engine binary 0.1.56 (locally built; `--kv` int8 patch + `STRATA_PARK_SWAP_GIB` ported from the fork).
  Why this shape: with `--batch-groups > 1` the engine refuses `--batch-mtp` unless `STRATA_PIPED_MTP` is set
  ("does not combine with --batch-groups yet"), and without batch-mtp batch slots cannot park. `=2` measured
  better than `=1` and `=4`. One slot per group keeps every stage busy.
  Three slots beat four on this box: four measured 139.4 tok/s mean and ~25 tok/s per stream with a
  19,388 MiB cache; three gives 144.6 / ~39 because each slot keeps ~3.5 GB more experts resident. The
  fourth slot only buys a fourth session resident versus one queued — the owner chose four anyway (2026-11-07)
  because parallel CAD/ops sessions queue otherwise.
  **Load-time trap:** the full-precision draft head needs 178 MiB on the last stage. With `--kv int8` and
  `--vram-reserve-mib 1024` at four slots it does not fit — the engine exits code 1
  (`mtp: the draft head does not fit`) and the wrapper crash-loops. `--mtp-q4 head` does not shrink that
  buffer. `k8v4` + reserve 512 fits with margin.
  Other measured arms (4 clients unless noted): 4 slots/4 groups k8v4 res 512 = 139.4 soak; 3 slots/3 groups
  res 512 = 144.6 soak (the live one); 3 slots res 1024 = 112 (90 s) · 22,953 → 20,905 MiB cache;
  4 slots/2 groups k8v4 = 86.3; 4 slots/4 groups piped 1 = 98.6; piped 4 = 89.0; `--kv int8` at 4 slots
  = 92.4 (costs ~3.6 GB of cache, not the 240 MiB per-cell arithmetic suggests). Reserve 384 loaded but
  died under load (`strata batch: N threads … moved off it`) — 512 is the floor.
  Historical: 2 slots/2 groups/piped 1 = 87.7 aggregate; 2 slots/1 group piped off = 34–39 tok/s per stream
  with drafts 0 (the drafter starves without piped MTP). Old 2026-11-05 baseline: 21,180 MiB cache,
  41–57 tok/s single stream.
- **Known failure mode (seen 2026-11-05 twice, under concurrent batch load):** engine exits code 1 with
  `verify [batch]: layer N never rang (unspecified launch failure / illegal memory access)` — upstream
  Strata bug in the verify/MTP batch path, likely tied to the piped-MTP experiment envs. The wrapper
  auto-restarts the engine ("a minute or two"); if it recurs, report at github.com/Niko1221/Strata with the active engine log
  (`strata-<profile>.log`, currently `strata-iq4xs.log`). Hindsight side is blameless — its retries only fail because the engine is down.
- Parking depends on the drafter: without `--batch-mtp` a batch slot's conversation is overwritten
  un-parked. Vision's reserve (1024) is the loose end — if images start failing, raise it and expect to
  lose a slot's worth of cache.
- **Binary is locally built and patched:** fork `tataetae/Strata`, branch `clara/pw-with-batch`
  (`STRATA_PW_WITH_BATCH=1` keeps `--pipeline-windows` for a lone request beside batch slots; upstream
  PR #3095 unmerged). Built sm_86 CUDA 13.1, installed at `engine/strata`; release backup
  `engine/strata-0.1.52-release.bak`.
- **Router (`STRATA_ROUTE_PRIOR`) must never be 0** (12 GB-card residency bias; default 1.5).
- Rejected-by-measurement configs are in Hindsight — recall before retrying anything.
- Bearer key required on `/v1/*`: `~/.config/opencode/strata.key` (+ `strata.env`, both 0600). `/health` is public.
- Experts in RAM via pinned arena (`STRATA_ARENA_PIN_GIB=32`); parking admission reads `MemAvailable`,
  not swap. Stop `freecad-gui` if RAM is needed.
- Sampling: temp 0.6, top_p 0.95, top_k 20, min_p 0, presence 1.5, repeat 1.0.
- Do not restart to "apply" config changes mid-chat; the profile is read once at start, and the learned
  profile `data/expert-profile-learned.bin` is already live. Engine config changes need a restart, but
  restarts are **owner-timed**.
- **Streaming:** gateway SSE keepalive 5 s (`HERMES_SSE_KEEPALIVE_SECONDS` drop-in). Client disconnects
  were the desktop's, not the engine's. 2026-11-12 hardening: keepalive 3 s, SSE write timeout 5 s → 30 s
  (`HERMES_SSE_WRITE_TIMEOUT`, code default 30 in `api_server_runs.py`), and `_sse_headers` now pins
  `Content-Encoding: identity` + `Connection: keep-alive` beside `X-Accel-Buffering: no`. Measured through
  the engine: first byte 0.01 s, median inter-chunk 0–23 ms, p90 54–64 ms; occasional 0.6–1.6 s stalls are
  expert-cache misses, not transport. The gateway needs an API key for an end-to-end test and none is
  stored on the box — the health API probes `/v1/models` with the strata key and gets 401 once a minute.
- **Hindsight policy:** no time windows. Engine runs **2 slots**; Hindsight shares them through the
  priority gate with per-type caps in `/etc/hermes-hindsight.env` (LLM_MAX_CONCURRENT=1, retain=2,
  reflect=1, consolidation=1 — live daemon env, 2026-11-06). Recall is a DB query and never queues
  behind generation.
- **Trust `--help`, not `strings`**, when checking whether the engine supports a flag.
- Configs `local-inference/strata-src/strata-iq4xs.json` (live since 2026-11-19) plus `strata-q4_kxl.json`,
  `strata-q3_xl.json`, `strata-iq3_s.json` (rollbacks);
  restore point `data/cfg-iq3s-KEEP-before-xxs.json`. GPUs 0–3, PCIe x16/x4/x4/x1; card 3 (x1) is the weak link.

## Overnight 2026-11-18 → 19 — cleanup state (system-maintainer)

- **Hermes freeze lifted** after the opencode session hit its API limit: `dispatch_profiles` removed,
  `dispatch_in_gateway: true`, and the **21 cron jobs that were live pre-freeze resumed** (identified by
  `last_run_at >= 11-17`, not guessed). The other 16 stay paused — they were already paused before.
- **`hermes-hindsight` was down 17:28 → 20:55 UTC.** opencode stopped it on purpose; it then could not come
  back for three stacked reasons: pg0's bundled ICU `.so.70` no longer resolving after the system moved to
  ICU 78, `postgresql.conf` naming a locale the OS no longer has, and the cluster catalog pinning
  `LC_COLLATE "en_US.UTF-8"` (that locale vanished on 11-14 06:08, same minute as `/var/crash/202611140608`;
  the long-running cluster hid it until something forced a restart). Full path is in `hindsight-memory-stack`.
- **Repaired without root**: `~/.pg0/installation/18.1.0/bin/postgres` is now a wrapper exporting
  `LD_LIBRARY_PATH` (bundled ICU) and `LOCPATH=/home/clara/.locale` (private locale compiled with `localedef`),
  then exec-ing `postgres.real`. **That wrapper is load-bearing** — a pg0 upgrade replacing `bin/postgres`
  brings the failure straight back. Clean fix when the owner wants it:
  `sudo locale-gen en_US.UTF-8 && sudo update-locale`, then delete the wrapper and restore `postgres.real`.
  `postgresql.conf` now has `lc_messages/monetary/numeric/time = C.UTF-8` (backup
  `postgresql.conf.bak-locale-20261119T0235`) — formatting-only.
- **Still awaiting the owner's call:** the armed hardware watchdog (`RuntimeWatchdogSec=30s` — a systemd stall
  hard-resets in 30 s). Engine left as opencode abandoned it: `PIN_GIB=70`, `--parallel 3`, split `11,23,41`.
  No kanban board or card state was touched. (The hugepages file now reads `0` — that landmine is gone.)

## Overnight 2026-11-19 01:00 → 04:00 — tuning sweep A/B/C + card mapping (opencode session)

Owner order: items 4/5/6/7 first, hardware work (1/2/3) tonight at the machine.
**Everything below was measured on this box.** Harness: `/tmp/opencode/measure.py` (cold prefill) and
`/tmp/opencode/soak.py` (decode). **Read the engine's own `decode_tok_s`, never the client wall clock** —
the wall clock includes the prompt read and queue time and understates decode by 3–5×.

- **Arm A — layer split `11,23,36`: REGRESSION, reverted.** CUDA3 (layers 36–47) also carries the head and the
  3 MTP drafters, so it only had **663 slots** (was 1,129) and the prompt chunk collapsed **6,144 → 2,560**:
  prefill **146.9 tok/s** (vs 341.5 control), aggregate decode 10.7. The engine's own split search deliberately
  equalises the three stages (~1,130 slots each) — do not hand-tune the split.
- **The 1.1 GiB MTP draft layer follows the FASTEST card**, not the last stage. Moving the x16 card earlier
  moved the draft layer with it, starved that stage (chunk 6,144 → 3,072) and cost 1.1 GiB of its expert cache.
- **Arm C — `--vram-reserve-mib 512`: HARD FAIL.** CUDA3 fell to 19 MiB free → `cublasGemmEx: cuBLAS status 14`
  (out of memory) → the engine reported `loaded:false` and every request failed. 1024 is the floor: the engine
  plans to keep 768 MiB free per card and 512 breaks that invariant.
- **Arm B/D — 4 slots (`--parallel 4` + `--batch-groups 4`): worse, reverted.** CUDA2 fell to 701 slots, chunk
  **2,816**, prefill **205.4** (vs 341.5), aggregate **33.0 with 4 streams** (8.2/stream) vs the 3-slot control's
  **34–64 aggregate** (11–21/stream). One more resident session for ~40 % worse prefill — keep **3 slots**.
- **Card mapping A/B: the engine's own order wins.** `gpu` in the profile maps stage→card; `gpu_roles` mirrors
  the list, so a hand edit is ignored unless the mirror is broken. Mappings tried: `[1,3,2,0]` (engine's speed
  order: helpers slowest first, main last) **56.7 aggregate** vs `[3,2,1,0]` (x1 card moved to the prompt stage)
  **50.7** vs `[3,1,0,2]` (chunk collapsed to 3,072). **PCIe is not the limiter**: `nvidia-smi pmon` shows the
  x1 card is the *least* busy GPU (36–54 % SM) while the 18-layer stage runs 74–98 %, and the decode breakdown is
  `57.9 ms/window = CPU 29.9 + GPU-reach wait 4.3 + PCIe 0.42/layer-window`. The wall is **CPU expert compute
  fed by DRAM at 34.9 GB/s** — which is why RAM at 4800 MT/s (item 2) is the 2.2× lever, not the links.
- **Live config now** (profile `strata-iq4xs.json`, IQ4_XS since 2026-11-19; unit env `STRATA_PIPED_MTP=4`,
  `STRATA_PIPED_CONCURRENT=2`, `STRATA_LOOKAHEAD_K=16`, `STRATA_FILL_AHEAD=1024`): split `11,23,41`, `--parallel 3`, `--batch-groups 3`,
  `--vram-reserve-mib 1024`, **`--conversation-cache-mib 24576`**, **`--conversation-cache-min-free-mib 32`**,
  `--conversation-cache-slots 16`, `gpu [1,3,2,0]`, `PIN_GIB=70`, `STRATA_PARK_SWAP_GIB=256`.
- **Conversation cache (item 5) needed TWO changes.** The budget alone made things *worse*: the engine filled RAM,
  its own `available` sample fell to 254 MiB and it refused to park ("skip parking (RAM floor after capture:
  256 MiB needed, 254 MiB available, source cgroup)") — so conversations were not saved at all. The floor is the
  knob: **256 → 32 MiB** and parking works again (`parked=15, evictions=0`, restores=26). The engine's admission
  counts free swap as headroom (`STRATA_PARK_SWAP_GIB=256`), so the surplus parks into swap instead of evicting.
  cgroup `memory.max` raised 104 → **112 GiB** (`systemctl set-property`, persisted in `system.control`); `memory.high`
  stays 100 GiB as the reclaim trigger.
- **Item 6 — the kanban "OOM" was a deferral, and the override was on the wrong unit.** The dispatch tick runs in
  **`hermes-dashboard.service`** (the process holding `agent.log`), not the gateway — so the gateway's
  `HERMES_KANBAN_IGNORE_MEMORY_PRESSURE=1` never applied. Added
  `/etc/systemd/system/hermes-dashboard.service.d/90-kanban-memory.conf`; deferrals **0** since.
- **GPU1's PCIe link is faulty** (identity: nvidia-smi **index 1** = PCI `03:00.0`, device ID `10de:2504`, the
  "Lite Hash Rate" SKU — the other three are `10de:2503`). Root port `00:1a.0` has **630 correctable AER** this
  boot (**2,594 kernel lines** vs 2 on every other port), still climbing; the card's own endpoint counters stay
  clean (RxErr 0) → the fault is in the **riser cable/connector**, not the GPU. It is *not* a throughput problem
  (that card delivers 6.4 GB/s, same as its x4 twin) but it is a reliability one. **Item 1 tonight: reseat/swap
  that riser**. Count AER from the journal (`journalctl -k -b`), not `dmesg` — the ring buffer flushes.
- **Hermes fixes this session:** `conduit_push` updated `eb0c854 → a2bc487` (**0.178.3 → 0.179.2**, `hermes plugins
  update conduit_push`); `clara-vitals` fixed (missing `__init__.py` → it failed on *every* start; now validates
  and loads); **Boxcat = the `system-maintainer` profile's housekeeper bot** ("Message from 🤖 Boxcat Housekeeper
  (@system-maintainer)") and it now has the **`hindsight` plugin** enabled (bank `hermes`), so Boxcat can
  recall/retain/consolidate. Hindsight itself is healthy: 2,575 nodes, 0 failed ops, 8 pending consolidations
  (worked with `POST /v1/default/banks/hermes/consolidate`).
- **Health check:** `bash /home/clara/logs/health_check.sh` (read-only, exit 1 on FAIL) and the human sheet
  `/home/clara/logs/HEALTH-CHECKLIST.md`. Both cover engine, GPUs, AER, memory/PSI/swap, every Hermes unit,
  Hindsight bank, plugin load errors, watchers and disk.

## 2026-11-19 05:00 → 06:30 — Hermes deep dive, conduit, Boxcat (opencode session)

- **Boxcat = the `system-maintainer` profile's housekeeper bot** ("Message from 🤖 Boxcat Housekeeper
  (@system-maintainer)", delivered into the Conduit rooms). It is working: 13/17 cron jobs enabled and ticking
  (ticker 1 min old), it has **completed cards on the shared kanban board** (`t_e3507ae9`, `t_2a88090a`), and it
  now has the **`hindsight` plugin** enabled (bank `hermes`) — the profile's `plugins.enabled` is per profile, so
  the plugin had to be added there, not at the root.
- **The kanban "OOM" was two different things.** (1) The memory-pressure deferral — the dispatch tick runs in
  **`hermes-dashboard.service`**, not the gateway, so the gateway's override never applied; fixed with a drop-in
  there, deferrals now **0**. (2) A separate `kanban dispatcher stuck: … 0 workers spawned` warning that ran
  03:30→05:03 and **recovered on its own** (workers spawned at 05:07 and 05:31; no warning in the last 30 min).
  The board is healthy: `hermes kanban list` shows done/running/blocked cards across software, reviewer, qc, plc,
  manufacturer and system-maintainer.
- **`hermes-hindsight`'s 159 restarts are history, not a live fault.** The crash loop was the maintainer's
  17:28→20:55 ICU/locale repair; the current process has been up 8 h 51 m with **0 restarts since 21:00**. Judge
  it by the process age, not systemd's cumulative `NRestarts`.
- **The priority gate holds requests while the engine is busy** — *including* `/health`. A timeout on `:8082` is the
  gate working, not a fault; check the process and the listening socket instead. (Same for
  `strata_clamp_proxy` on `:8081`.)
- **Conduit plugin:** updated to `0.179.2` (`a2bc487`). Its "Bot Chat isn't under its gateway yet (unsupported:
  ModuleNotFoundError)" is **pre-existing** (776 log lines before the update) and unfixable here: this Hermes
  (v0.22.2+196.gaee2bac.dirty, 58 locally modified files, HEAD `aee2bac`) has no `hermes_cli/gateway_client.py`,
  `gateway_runtime.py` or `gateway/session_controls.py` — upstream moved them in later releases. **Local patch
  `6bd0b00`** reports it through the plugin's own `degraded` codes (`bot-chat-adoption`, which Conduit's plugin
  page shows) and logs it at DEBUG instead of INFO every sweep. Revert: `git reset --hard a2bc487`. The plugin's own
  `plugins validate` **fails on the pristine tree too** (private core imports, in-process isolation) — pre-existing.
- **Conduit health:** relay `https://push.milim.dev` reachable, E2E credentials valid (mlkem768+x25519), the phone
  has contacted the `system-maintainer`, `plc` and `software` profiles in the last 4 h.
- **Bot reply latency is the engine, and it has two parts.** (1) A *fresh* read costs a fixed ~15–25 s whatever
  its size — the engine's own log: `read 325 tokens … in 15889 ms`, `read 1322 tokens … in 29163 ms` — because each
  prompt read borrows a chunk of expert slots across all four stages. So even a 98 %-reused continuation waits
  16–38 s for its first token. (2) A conversation that the cache evicted pays a full cold prefill: 40,915 tokens
  in 85.7 s, 91,667 tokens in 217.6 s. The eviction churn is the part worth fixing: the cache holds ~15
  conversations at ~1–1.8 GiB each and the 24 GiB budget is full, so admissions evict 1–6 conversations at a time.
- **`/home/clara/logs/hermes_health_check.sh`** — the full Hermes check (services + process age, every endpoint,
  18 profiles, Boxcat's hindsight plugin, conduit link + relay + version, cron per profile with ticker age, kanban
  board, Hindsight bank, log watch, engine dependency). Currently **19 pass, 1 warn, 0 fail**.

## 2026-11-19 05:50 → 06:10 — parking slots on the NVMe swap (opencode session)

Owner asked for the parked conversations to live on **NVMe swap** and for **~12 GiB to stay free** for the host.

- **All swap is already NVMe**: `/swap.img` (8 GiB, root LV) + `/srv/inference/swap/swapfile` (56 GiB) +
  `swapfile3` (48 GiB) — all on `nvme0n1` (`ubuntu--vg-inference`, ROTA=0). 112 GiB total, ~86 GiB free.
- **The lever is the engine cgroup's `memory.high`, not swappiness.** Set it with
  `systemctl set-property strata-iq3s-server.service MemoryHigh=84G` (persisted in `system.control`;
  `memory.max` stays 112 GiB as the OOM line). The engine's RAM is then held at 84 GiB and the kernel reclaims
  the cold parked snapshots into the NVMe swap.

  | | before | after |
  |---|---|---|
  | engine cgroup RAM | 96.2 GiB | **84.0 GiB** (pinned at the high) |
  | engine cgroup swap (NVMe) | 18.6 GiB | **24.2 GiB** (the parked conversations) |
  | MemAvailable | 756 MiB | **12.5 GiB** |
  | PSI after the one-off reclaim | — | 0.02–0.16 |

  The one-off reclaim spike was **PSI 25 for ~20 s** (it is the kernel pushing pages out, not a stall);
  restores stayed ~300 ms.
- **`--conversation-cache-mib` raised 24,576 → 40,960 MiB** (profile) so the slots *fit* now that the surplus
  lands on NVMe: parked=7 (4.6 GiB) with **evictions=0** after the change. The engine's admission already counts
  free swap as headroom (`STRATA_PARK_SWAP_GIB=256`), so a bigger budget parks more rather than evicting.
- **zswap: no.** It is compiled in but disabled (`enabled = N`), and there is nothing to save — the NVMe write rate
  during reclaim measured **0 KiB/s**. It would also spend the RAM we are freeing (compressed front cache in RAM).
- **zram: no — already rejected by the owner** in `/etc/sysctl.d/99-strata-parking.conf`: "zram is a compressed RAM
  device with random access: reading 8 pages per fault wastes work" (hence `vm.page-cluster = 0`). It would also
  burn the CPU the engine needs for expert compute (29.9 ms/window) to save RAM we do not need to save.
- **The swappiness conflict is still there**: `99-strata-parking.conf` sets `vm.swappiness = 100` and
  `99-zz-clara-stability.conf` overrides it to **60**. The cgroup-level `memory.high` makes the swap preference work
  *without* touching the global setting — leave it at 60 unless the owner decides otherwise.
- **`--conversation-cache-min-free-mib` had to go 256 → 32 → 0.** With `memory.high=84G` the engine's RAM sits *at*
  the high, so its own cgroup room oscillates around 0 and the floor check ("skip parking: 32 MiB needed, 4 MiB
  available") refused to save conversations. At **0** the cgroup high is the only governor and parking works
  (parked=10, evictions=0). The engine's engine swap is now **8.5 GiB** — the parked conversations really are on NVMe.

## 2026-11-19 07:00 → 09:30 — interleaving sweep (owner: "do this now")

Harness: `/tmp/opencode/armtest.py` (results in `logs/armtest-results.jsonl`). Three metrics: cold prefill, decode
with 3 clients (engine-side `decode_tok_s`), and **decode rate during a long prompt read** — the metric that
matches what the owner actually feels.

| arm | change | during-read | notes |
|---|---|---|---|
| M0 control | — | **0.09 tok/s** (59 tok in 667 s) | prefill **332.0 tok/s**; the read ran 215 tok/s because the slots shared the GPUs |
| M1 | `STRATA_PIPED_READ_PIECE=6144`, `_DECODE_MAX_MS=10000`, `_DECODE_MIN_MS=3000` | **2.05 tok/s** | the slots now decode **9.5–19.5 rows/s during reads** (the control's reads often showed 0 rows); read cost ~3 % (418 vs 430 tok/s) |
| M2 | + `STRATA_PIPED_MTP=4` | **2.84 tok/s** | MTP acceptance 354/563 = 63 % (2 proposals gave 55–67 %); CUDA3 still 515 MiB free |
| M3 | + `--vram-elastic` | 0.05 tok/s (same config as M2!) | see the confound below |

- **The end-to-end number is confounded by SLOT ADMISSION, not GPU contention.** The same config (M2/M3) measured
  2.84 and 0.05 tok/s for the same 40–60 token probe. The engine reported **`running=6` with 3 slots**: the cron
  fleet holds every slot for **500–1000 s per request**, so a new request waits for a slot no matter how the GPU
  time is arranged. Per-boot medians of "rows/s during a read" vary 0–13 with the cron load, not with the config.
  **The wait is a slot problem, not an interleaving problem.** The interleaving arms still improve the engine's
  behaviour during a read (real, reproducible) but cannot fix admission.
- **Arm A (`--expert-cache-per-layer`) was skipped on purpose.** Its documented win (2.97 % → 54.4 % hits, CPU pool
  drain 19.1 → 10.3 ms/token) is for a *shared-counter* cache on one GPU; our split stages each hold their own
  profiled pairs and already run at **67–85 % hits**, so the premise does not hold here.
- **Arm D (`--vram-elastic`) is enabled but only usable in an idle window**: `POST /v1/vram {"reserve_mib": N}` answers
  **"VRAM: not while batch slots are decoding"**, and the elastic cache moves in **512 MiB segments** — so the only
  reachable reserves are 1024 (safe) and 512 (the `cuBLAS status 14` OOM). It needs `--vram-segment-mib 256` to have a
  middle step. Not worth the risk for ~+80 slots/stage.
- **Two mistakes to not repeat**: inserting a flag *between* another flag and its value gave
  `unknown argument: 1024` and the engine was **down 08:34→08:47** (the unit's start limit hid it); and `pkill -f`
  matches the calling shell's own command line (twice).
- **Left running**: 3 slots, `STRATA_PIPED_MTP=4`, `STRATA_PIPED_READ_PIECE=6144` (+ the two decode-window vars),
  `--vram-elastic`, conv cache 40,960 MiB / floor 0, cgroup high 84 GiB / max 112 GiB.
- **If the wait must go, the lever is slot count or cron load**, not interleaving: 4 slots was measured worse for
  prefill (205 vs 341) but gives a 4th resident session; the cron fleet's long requests are the other half.

## 2026-11-19 09:30 → 10:00 — the build switch (owner: "use the best Q3 (largest)")

**Live model is now `UD-Q3_K_XL`** (profile `strata-q3_xl.json`, ExecStart points at it). `strata-q4_kxl.json` is
the Q4 rollback (same flags, Q4 paths); `strata-iq3_s.json` is the older GSQ-RCO IQ3_S config.

| build | total size | expert slots | cold prefill 16k | decode, 3 clients | note |
|---|---|---|---|---|---|
| `UD-Q4_K_XL` (was live) | 103.7 GiB | 6,449 | 332.0 tok/s (869.8 s wall) | 20–37 aggregate | best quality |
| **`UD-Q3_K_XL`** (live) | 83.3 GiB | **9,242** | **122.9 s wall** | **54.9 aggregate** (18.3/stream) | the largest Q3 |
| `GSQ-RCO-IQ3_XXS` | 70.6 GiB | **13,080** | **754.5 tok/s** (86.5 s wall) | **68.9 aggregate** (23.0/stream) | **owner rejected it: hallucinates** |

- **Why Q3 is faster: resident experts, not bytes.** The expert blobs are 41.4 GiB (Q4) / 44.5 (XXS) / 44.7
  (Q3_K_XL) — nearly the same. The speed comes from how many fit in VRAM: 6,449 → 9,242 → 13,080 slots, which
  sets the hit rate and therefore the **CPU expert work** (29.9 ms of a 57.9 ms window). That is why the Q3_K_XL
  beats the Q4 on decode despite carrying *finer* experts (IQ3_XXS + Q5_0 + IQ4_NL + IQ4_XS).
- **The repo ladder** (`unsloth/Qwen3.8-Flash-Next-GGUF`): IQ1_S 61.8 · IQ1_M 63.3 · Q2_0 65.7 · IQ2_XS 70.6 ·
  Q2_K_XL 74.1 · IQ3_XXS 77.9 · **Q3_K_XL 83.3** · IQ4_XS 87.2 · Q4_K_XL 103.7 · Q5_K_XL 147.4 GiB.
  **There is no IQ3_M in either repo** (ISTA-DASLab GSQ-RCO has only IQ2_XS/IQ3_S/IQ3_XXS). The owner's floor of
  "IQ3_M" would be the GSQ-RCO **IQ3_S** (77.9 GiB, on disk at `models/IQ3_S/`) or **UD-IQ4_XS** (87.2 GiB).
- **Pack building** is cheap: `tools/iq_pack.py --gguf <shard 1> --out <packs dir> --compat-bf16` → 1.4 GiB
  (dense.bin + index + conversions); the experts stay in the GGUF shards.
- **`--expert-cache-per-layer` was skipped**: its documented win (2.97 %→54.4 % hits) is for a shared-counter cache
  on one GPU; our split stages already hold their own profiled pairs.
- **`--conversation-cache-min-free-mib` went 256 → 32 → 0**: with the cgroup high at 84 GiB the engine's own cgroup
  room oscillates around 0 and the floor check refused to save conversations. At 0 the cgroup high is the only
  governor; parking works (parked=7, evictions=0) and the engine's swap holds the parked conversations on NVMe.
- **Boxcat's cron spam stopped**: paused (reversible, `hermes cron resume <id>`, with
  `HERMES_HOME=/home/clara/.hermes/profiles/system-maintainer`) — `crash-alert-1min` (every 1 m), `boot-resume`
  (2 m), `engine-tok-watchdog` (5 m), `agent-watch` (10 m), `clarabuild-tabslot-watch` + `cad-pending-watch`
  (20 m), `p14-kanban-warden` (hourly, the top phone spammer with 15 deliveries). **Still enabled**: the daily
  hindsight check (03:00), `stability-hourly` (30 m), `engine-perf-hourly` (hourly), `nightly-maintenance` (19:00),
  `backup-freshness-watch` (2 h), `workspace-prune-weekly`.
- **Compression threshold 204,800 → 150,000** in the root config and all 15 profiles (16 files).
- **The engine's own RAM demand dropped** with the smaller model: MemAvailable is now **32 GiB** (the arena pin is
  capped by the model size), so the 84 GiB cgroup high is no longer the binding constraint.

## 2026-11-20 11:00 → 11:40 — engine-shape review on UD-IQ4_XS (owner: "maximum throughput, Q4 XS or better only")

**The window is now ~27 ms** (the Q4 build's was ~58 ms — the build switch was the 2×). Breakdown per window
(engine's own `STRATA_DECODE_TIMING=1` line):

```
verify 26.6-27.4 ms = GPU-reach wait 4.7-4.8 + per-layer host 0.1-0.5 [CPU expert 5.0-5.5] + stage 0.0-0.2
             + commit/emit 0.8 + draft 0.0-1.2 + adapt join 0.0-1.3
per layer-window: CPU experts 1.3-1.9 entries, VRAM hits 8.1-8.7, PCIe 0.00
```

- **PCIe is confirmed irrelevant**: `PCIe 0.00` per layer-window and `pcie_share` 0.0-2.7 % — nothing crosses
  the links. The owner's instinct was right.
- **`--adapt-min-gain 2.5 -> 6` (KEPT):** the adaptive tier's churn stops (**96 -> 0 swaps/window**), the CPU
  expert time falls 5.8-8.0 -> 5.0-5.5 ms and the window 29-33 -> 26.6-27.4 ms. The hit rate is unchanged
  (0.81-0.88) and the profile is re-learned every 10 min, so the cache still tracks the conversation.
- **`memory.high` 84 -> 92 GiB (KEPT, live):** the engine's anonymous need (arena 43.7 + conv cache ~25 + working)
  exceeded the old high, so it lived in permanent reclaim — **parks took 17.5 s and evictions hit 406**. With the
  headroom: parks 548-2,149 ms and **0 evictions**. The host keeps ~10 GiB.
- **Dead ends, with the reason:**
  - `--kv-resident` (KV streaming to RAM to free VRAM for experts) **needs `!multi_gpu`** (`generate.cpp:5884`),
    so it cannot be used with a layer split.
  - The **layer split cannot be hand-tuned**: the engine's split search overrides the requested value
    (asked 11,23,36, it ran 11,23,41) and there is no steering flag (`--split-skip-if-fits` is the only one).
    It equalises the *expert slots* per stage and does not account for the last stage also carrying the head,
    the MTP draft layer and the 3 slot drafters.
  - **4 slots**: measured worse (chunk 2,816, prefill 695 s) — reverted.
- **What the window is made of now:** GPU compute ~14.6 ms (the model's dense path — not configurable), GPU-reach
  wait ~4.7 ms (the 4-stage hand-off), CPU expert ~5.3 ms (**DRAM-fed → tonight's RAM speed is the lever**),
  the rest ~4 ms. The remaining big items are therefore the *hardware* ones: the CPU expert path (RAM at
  4800 MT/s) and the GPU1 link (which sits in the hand-off path).

## 2026-11-20 11:45 → 12:10 — KV streaming works WITH the layer split (the big win)

**Correction to an earlier claim of mine:** the `!multi_gpu` gate at `generate.cpp:5884` is for **`--kv-grow`**
(the *elastic* K/V), **not** for `--kv-resident` (KV streaming). `qsa_set_kv_resident()` has no multi-GPU gate and
the per-stage `chunk_bytes(lb, c)` already accounts for the split. **KV streaming works on our 4-GPU split as-is —
no compile needed.**

**`--kv-resident 32768` (KEPT, in the profile):**

| | before | with KV streaming |
|---|---|---|
| expert slots | 8,758 | **12,835** (+47 %) |
| CUDA2 slots | 1,433 | **2,865** (doubled) |
| prompt chunk | 5,632 | **8,192** |
| CPU expert time / window | 5.0-5.5 ms | **2.54 ms** |
| CPU expert entries / layer-window | 1.3-1.9 | **0.44** |
| hit rate | 0.81-0.88 | **0.956** |
| decode, 3 clients | 40.9 aggregate | **60.6 aggregate** (20.2/stream) |
| reply during a long read | 1.19 tok/s | **3.48 tok/s** |
| fresh-read rate (engine's own) | 57-94 tok/s median | **233 tok/s** median |
| pinned RAM cost | — | **0.40 GiB** |

The freed VRAM goes straight into the expert cache (`--vram-reserve-mib 1024` is unchanged and the per-card free
VRAM is the same as before: the KV's VRAM became expert slots). The streaming itself is nearly free: "61-89 % of
the block reads hit VRAM, 0.2-7.9 MiB read from RAM".

**Why `--kv-grow` (the elastic K/V) stays single-GPU** — it is the *opposite* lever: the whole K/V stays in VRAM
and the **expert cache gives slots back** as the context grows (the experts move to RAM and the CPU computes them).
Its gates (`generate.cpp:5884`): `!multi_gpu && kv_resident <= 0 && profile && !resident_cpu_experts &&
expert_cache != 0 && !remote && vmm_available() && batch == 0 && !vram_elastic && peer_device < 0`. What makes it
single-GPU: (a) the VMM helper creates the K/V's address range on **one device** (`src/core/vmm.cpp`:
`prop.location.id = a.dev`, one `VmmChunk` per device) and the elastic path grows *that* range in place;
(b) "give slots up" is one `ExpertCache` (one per device) while a split has four stages with four caches;
(c) in a split the K/V lives in per-stage sessions, so the growth would have to be coordinated per stage; and
(d) for us `--batch 3` also disables it — the batch slots carve their own K/V.

**Compiling here is possible** if a further experiment is wanted: CUDA 13.1 (`/usr/local/cuda-13.1`), cmake, ninja,
gcc and the tree's `build-cuda` script are all present; the engine is the local fork `tataetae/Strata`, branch
`clara/park-swap`, HEAD `74fd955f3`, with 65 locally modified files.

## 2026-11-20 14:30 → 15:30 — the NVMe parking work, and what the drafter was missing

**Conversation cache → the NVMe.** The eviction was never a RAM-pressure problem: the cache's *logical* budget
(`--conversation-cache-mib`) is the cap, and the engine counts every parked conversation against it whether the
pages are resident or swapped. Now: budget **262,144 MiB (256 GiB logical)**, the engine cgroup's `memory.high`
**80 GiB** (the RAM side), `memory.max` **136 GiB**, and the swap grown to **278 GiB** (added
`/srv/inference/swap/swapfile4`, 160 GiB, on nvme0n1; the owner authorised up to ~400 GB on /srv/inference). The
engine's floor is **~70 GiB** (56.2 GiB pinned anon: the arena + the CUDA pinned buffers + the KV host copy, plus
13.7 GiB of *mapped* GGUF pages that cannot be dropped without unmapping). **Never set `memory.high` below that** —
at 60 GiB the engine sat in permanent reclaim (PSI memory 95 %, health endpoint timing out) and had to be rescued by
putting it back to 80.

**The stale IQ3 build is gone** (`models/IQ3_S` 78 GiB + its pack 1.5 GiB + the dangling `strata-iq3_s.json`
profile). The rollbacks that remain are `strata-q4_kxl.json` and `strata-q3_xl.json`.

**What the drafter could not say.** The shipped draft subset (`mtp/rt/draft_vocab.bin`, 106,299 of 248,320 ids)
holds all han/kana/hangul + English/code — **and essentially no Thai**: against a corpus of the owner's own Thai
(64,515 fragments, 354,142 token occurrences) **79.73 % of the token occurrences were outside the subset**. A
Thai-aware subset was built with the tree's own tool:
`tools/draft_vocab.py --gguf <IQ4_XS shard 1> --base mtp/rt/draft_vocab.bin --corpus <thai> --coverage 0.99
--out mtp/rt/draft_vocab_thai.bin` → **+1,885 ids** (108,184 total), **corpus occurrences outside: 79.73 % → 1.00 %**,
cost **+15 MiB** of draft-head VRAM. (The character-level count stays 60/96 — the BPE merges multi-byte pieces, so
the corpus metric is the meaningful one.)

**Staged for the next engine restart** (the owner is working in Hermes, so nothing was restarted):
- `--mtp-draft-vocab /srv/inference/models/qwen3.8-flash-next/mtp/rt/draft_vocab_thai.bin` — the drafter can now
  propose the Thai the owner actually writes → a much higher MTP acceptance on Thai → faster replies.
- `--kv int8` (was `k8v4`): 816 → 1,056 B/cell. The K/V lives in pinned RAM now, so the cost is only the VRAM ring
  (1.93 → 2.51 GiB = **~251 expert slots**, ~2 % of 12,835; the hit rate moves ~0.4 %) and the host copy grows
  0.40 → 0.51 GiB. It buys 8-bit V instead of rotated-Q4 V — better fidelity over long Thai documents, and it is
  still above the owner's precision floor (`q4_0` was rejected for garbled tokens).
- `STRATA_IQ_MT_MIN=1` — the documented fix for the IQ rounding difference, i.e. a verified token no longer
  depends on whether it was drafted. **This is the leading suspect for the Thai leak**; cost IQ decode −1 to −3 %.
- `--kv-resident 20480` (the minimum): the K/V's VRAM ring shrinks, the freed VRAM goes to the expert cache
  (~+320 slots).

## 2026-11-20 16:30 → 17:10 — the stale-model bug, the summariser, and the profile audit

**The compaction 404 was a STALE MODEL NAME, and it leaked to the cloud.** The dashboard log at 10:54–11:04
showed `Compression summary failed: Error code: 404 - model "qwen3.8-flash-next-q4_k_xl" not found. Try
glm-5.3-flash:cloud.` — that error text is **ollama.com's** (reproduced with a direct curl), so a conversation
transcript was offered to a CLOUD provider. Root cause: the ROOT profile's `auxiliary.compression` had been
pointed at `ollama-cloud / deepseek-v4.1-flash:1017` (see `config.yaml.bak-20261118-0830-compaction`), and
**571 session rows still carried deleted build names** (`qwen3.8-flash-next-iq3_s` × 545,
`qwen3.8-flash-next-q4_k_xl` × 25, `qwen3.8-flash-next-q3_xl` × 1). A session whose stored model is a deleted
name routes the summary to a provider that does not have it → 404 → the transcript never summarises.

**Fixed (measured, not assumed):**
- **571 stale-model sessions repointed** to `qwen3.8-flash-next-iq4_xs` via the supported `SessionDB.update_session_model`
  (writes `model` + `model_config` in both shapes resume reads). Now **0 stale rows remain**.
- **1 cloud-routed session pinned local** (`software / 20261113_080350_054bae`, ollama-cloud → strata). Now
  **0 cloud-routed live sessions** remain, so no transcript can be offered to a cloud provider.
- **`/etc/hermes-hindsight.env` pointed at a deleted build**: `HINDSIGHT_API_LLM_MODEL=qwen3.8-flash-next-iq3_s`
  → `qwen3.8-flash-next-iq4_xs` (backup `hermes-hindsight.env.bak-20261120-model`); Hindsight restarted 16:35.
  It *worked* anyway because the engine does not validate the model name — but it was a landmine.
- **Every profile's `auxiliary.compression` is now `strata` / `qwen3.8-flash-next-iq4_xs`** (local). Two profiles
  (`codex-worker`, `codexworker`) have **no** auxiliary block at all — their summary falls back to the main model.

**Firing the summariser standalone.** `/compress` on the CLI sends the text to the LLM (it is not dispatched as a
slash command outside a TTY), and the pty route echoed it as a user message. The working route is the gateway's own
recipe (`gateway/slash_commands_session.py:_run_manual_compression`): build a throwaway `AIAgent` on the session's
own route, run the shared `compress_now()` core over the persisted transcript, commit the rotation. Tool:
`/tmp/opencode/fire_compress.py` + `run_compress.sh` (the runner must use Hermes' **managed install runtime**
(`/home/clara/.hermes/installs/*/environments/*/venv/bin/python -I` + the install's `site-packages` on the path);
the repo `venv/` is Python 3.11 and lacks `ruamel`/`pydantic_core` for the 3.14 runtime Hermes actually uses).
Proof it works: ROOT session `4865cff242b9` → **886 msgs / 585,699 tok → 98 msgs / 66,090 tok**, summary committed,
`aux_provider=strata` (no cloud traffic), one 10-minute local prefill per session.

**Cost:** each summary is a ~47k-token local prefill = **~10 minutes** of one engine slot. 27 live chat sessions
are over their profile's threshold (150k for most, 96k for system-maintainer).

**The Boxcat chat is NOT bloated.** `20261019_201306_f301bf` holds only **102 active messages / 34,811 tokens**
(the 40,696 messages / 38 MB includes 21,248 rows already `active=0`). Its compression is a **no-op** ("No changes
from compression: 68 messages") because the window is below the tail budget — the earlier "6.6M tokens" figure was
a miscount of `active=0` rows.

**The profile / bot / Hindsight audit (owner: "check ALL hermes profile and bot settings, hindsight too"):**

| finding | fix |
|---|---|
| 571 session rows carried a **deleted** build name | repointed to `qwen3.8-flash-next-iq4_xs` via `SessionDB.update_session_model` — **0 stale rows left** |
| 1 live session routed to **ollama-cloud** (`software/20261113_080350_054bae`) | pinned to strata — **0 cloud-routed live sessions left** |
| `/etc/hermes-hindsight.env` LLM model = the **deleted** `qwen3.8-flash-next-iq3_s` | → `qwen3.8-flash-next-iq4_xs`, Hindsight restarted 16:35 (backup `hermes-hindsight.env.bak-20261120-model`) |
| **5 cron jobs** in `system-maintainer/cron/jobs.json` named the deleted `iq3_s` | repointed (backup `jobs.json.bak-20261120T1725-model`) — **0 stale names in any cron** |
| `codex-worker` + `codexworker` had **no** `auxiliary.compression` → their summary falls back to the **cloud** main model | added a local `strata / qwen3.8-flash-next-iq4_xs` route (backups `config.yaml.bak-20261120T1715-comproute`) |
| every other profile's `auxiliary.compression` | already `strata / qwen3.8-flash-next-iq4_xs` — verified, all local |

- **Hindsight after the fix:** `/health` `healthy` (database connected), `/health/live` up, bank `hermes` = 2,754 facts,
  and `llm-requests/stats` shows **40 success / 0 error** in the recent window (before the fix, today had 26 errors).
- **Compression settings per profile are coherent**: `threshold_tokens` 150,000 everywhere except system-maintainer
  (96,000) and plc (0.75 ratio / 10 protected); `micro_compact: false`; `idle_compact_after_seconds` 900–1800.
- **The "0 of 0 drafts" in the engine log is a reporting artifact**, not a broken drafter: those DONE lines are
  single-token *checkpoints* (`1 generated in 38 ms`). The real batch counters show MTP healthy —
  `17037 of 26945 MTP proposals kept` (63 %), `10701 of 18286` (59 %).
- **The Boxcat chat is NOT bloated** — see above; its 40,696 messages are mostly `active=0`.

## 2026-11-20 17:50 → overnight — the summariser batch, Boxcat coordination, memory

**The batch is RUNNING in the background** (owner is asleep): `python /tmp/opencode/batch_run2.py`
→ `/tmp/opencode/batch_log.txt` (one line per session: profile, id, tokens, result, seconds).
27 live chat sessions are over their profile's threshold. Sequential, because each summary is a
~47k-token local prefill on ONE engine slot (~15–25 min each).
The runner is `/tmp/opencode/run_compress.sh`; it must use the **managed install runtime** (§ above).
`batch_run2.py` **skips any session that already has `compacted=1` rows**, so it is safe to restart.

**Measured savings** (tokenised with the model's own tokenizer):
`fea46266541b` 363,127 → 61,983 · `f598dbbacdd3` 360,717 → 78,846 · `d262cc989c33` 356,071 → 41,382
= **897,704 tokens freed** across three sessions; 14 of 27 were already compacted by 18:03.

**The watchdog lesson — `/health` TIMES OUT under load.** The first watchdog stopped the batch at 18:01
after a **single** 10 s `/health` timeout, while the engine was healthy (it answered in 0.00 s the
moment after). A busy engine legitimately times out on `/health` — the engine was mid-prefill on a
300k-token prompt. The fixed watchdog (`/tmp/opencode/watchdog.sh`) requires **3 CONSECUTIVE failed
probes** (8 s each), plus a PSI avg10 > 25 or MemAvailable < 2 GiB. The batch was restarted with
`batch_run2.py`, which skips what is already done — the killed run had actually completed 14 of 27.

**Boxcat was told — TWICE, via the live-owner mailbox.** `tools.bot_live_delivery.deliver_to_live_owner`
→ `runtime/bot_live_delivery/<id>.json`, status `queued`; claimed at the session's next turn boundary
(`tui_gateway/session_notifications.py` → `claim_pending_delivery`, every 5 s). The CLI path
`hermes chat -c "Bot Chat"` **refuses** while the owner's desktop window holds the session
(`SESSION_NOT_OWNED`) — do not use it for this. Prior deliveries on this mailbox show `settled`, so
the lane works; Boxcat was mid-turn, so both messages waited in `queued`.
Message 1 (`59cb6989…`, 4,268 chars): the 404 was a stale model name that leaked to ollama-cloud;
do NOT also fire `/compress` on a session the batch is touching; **its own chat is NOT bloated**
(102 active msgs / 34,811 tok — the 40,696/38 MB figure counts `active=0` rows); do not restart the
engine; do not touch `memory.high` (floor ≈ 70 GiB, it is 80).
Message 2 (`cfaccddb…`, 6,535 chars): the memory answer and the eviction analysis (below).

**The owner came back to the Boxcat chat at ~17:40** (msg id 60285): *"Hmmm. Memory pressure??
supposed to be like 20-30 gb avail? Ok coordinate with opencode and fix that overnight and then make
sure @software works. There should be no eviction: at most and always park to nvme. I want zero re
prefill on processed prompts!"* — so the answer to the memory question is Boxcat's to deliver:
- **There is no memory pressure.** PSI some avg10 **0.02**, swap 27 of 278 GiB used (251 GiB free on
  the NVMe), 0 OOM kills. The engine's cgroup is **pinned at 79.99 of 80 GiB** (`memory.high` 8700
  events = permanent reclaim) — that is the DESIGN (the arena + KV hold the RAM, the conversations
  live in swap). The 14 GiB MemAvailable is free 2.3 + buffers 0.9 + reclaimable cache 30.0 − overlap.
- **The "evictions" are NOT memory evictions — they are the SLOT LIMIT, proven in the source.**
  `include/strata/core/conversation_cache.hpp:395`:
  `while (!entries_.empty() && (entries_.size() >= slots_ || bytes_ > budget_ - held - incoming))
   if (!evict_oldest()) return false;` — so a 17th conversation evicts the oldest when
  `--conversation-cache-slots 16` is full, **regardless of the byte budget** (17–20 GiB of a 256 GiB
  budget in use). There is no *"evicted N parked conversations"* line anywhere in the engine log, so
  the physical-RAM admission gate in `generate.cpp` never fired. The counter DOES grow with load
  (42 → 43 → 44 across the batch), one per conversation that finds the cache full — and each such
  eviction is a **lost parked copy = a re-prefill next time**, which is exactly the owner's rule.
  The fix is `--conversation-cache-slots 16 → 64` (owner-timed restart), not more RAM.
- **"Zero re-prefill" = more SLOTS, not more RAM.** Measured reuse: 32827→32826 reused (100 %),
  41337→38734 (94 %), 35549→34679 (98 %), 32969→18435 (56 %); the 0 % cases are NEW conversations.
  A processed prompt is re-prefilled when its parked copy is gone — the 16-slot ceiling.
  `--conversation-cache-slots 16 → 64` needs an owner-timed restart.
- **Fixed for @software:** `profiles/software/config.yaml` had a bare `mcp_servers:` (null) key —
  the source of its repeated *"empty section(s): `mcp_servers` … silently drops"* warning. Now
  `mcp_servers: {}` (backup `config.yaml.bak-20261120T1820-mcpservers`). software is ALIVE (a turn
  10 min old); its own blocker is CONTEXT (session `20261113_080918_3ea80d`, 277 msgs), and that one
  is under the threshold, so it is not in the batch.

**Memory at handover (17:50):** MemAvailable **14.7 GiB**, PSI some avg10 **0.00** (avg300 3.13),
swap 24.8 of 278 GiB used, engine cgroup **79.5 GiB** (high 80, max 136), Hermes dashboard 1.0 GiB,
hindsight 1.5 GiB, conv cache 15.6 GiB parked (36 evictions, budget 256 GiB). **No pressure.**
The engine's floor is ≈70 GiB, so `memory.high` must stay at 80 GiB.

**Live in the Bot Chat while Boxcat works** — the batch skips `20261019_201306_f301bf` and anything the
gateway holds open; the rest are ROOT webui sessions (old, 1.2k–2.5k h) plus a few profile chats.

## 2026-11-20 18:28 → 19:00 — GPU3 FELL OFF THE BUS (Xid 79) and the engine crash-looped

**What happened.** At 18:28:19 the kernel logged `NVRM: Xid (PCI:0000:08:00): 79, GPU has fallen off the
bus` for the card at PCI **0000:08:00.0** (the engine's **CUDA3**, profile `gpu [1,3,2,0]`).
**Topology correction (verified 2026-11-21 from `lspci -D -t` + `nvidia-smi`):** `08:00.0` is the **x1**
card on root port **`00:1d.7`** (the M.2_2 riser); `00:1a.0` carries **`03:00.0` = GPU1**, the *Lite Hash
Rate* SKU (`10de:2504`, x4). The AER errors on `00:1a.0` had been escalating all evening (18:04:56,
18:15:49, 18:22:05) and `00:1d.7` threw a Physical-Layer AER at 18:28:19 — the escalation belonged to
**GPU1's** port, but the card that actually **died** was GPU3's on `00:1d.7`. After the reseat the
`00:1d.7` counters read **0** and the `00:1a.0` ones read 1 (RxErr) — so the reseat fixed the right
card and GPU1's link is still the marginal one.

**The engine crash-looped from 18:30 to 18:52 (exit code 2).** The cause was NOT a config bug:
`src/program/generate.cpp:3539-3560` derives `--split-device` from the **visible** card count
(`n_dev`) and requires `len(layer_split) == len(split_devs) == n_dev - 1`. With GPU3 gone,
`CUDA_VISIBLE_DEVICES=1,3,2,0` enumerates only **3** cards, but the profile's `layer_split` still had
**3** values (11,23,41) → mismatch → `--layer-split` rejected → exit 2 → the wrapper's retry loop.
The engine's own message spelled it out: *"needs --serve, rising K from 2, and one distinct GPU per K
in --split-device (1..2 …) … got 11,23,41"*.

**Recovery (no reboot, no physical access):**
1. `rmmod`/`modprobe` of the nvidia modules recovered the CUDA runtime: `nvidia_modeset` was held by
   `nvidia_drm` (no user process), and after the reload **`cuDeviceGetCount` returned 3** — the three
   live cards came back, the dead one did not. Hindsight had to be stopped for the reload (it holds
   the module) and restarted after (models take ~7 min to load: embeddings + cross-encoder on CPU).
2. Profile changed to a 3-GPU shape: `gpu [1,2,0]` (main card 0 last), `gpu_roles {1:helper,2:helper,
   0:main, 3:off}`, `layer_split: "auto"` (the engine's own search — do not hand-tune). The 4-GPU file
   is preserved as **`strata-iq4xs-4gpu.json`**; restoring 4 GPUs is one copy.
3. Engine back up 18:52:44, **3 GPUs**: split `CUDA0 / CUDA1 13-31 / CUDA2 32-47`, **7,235 expert
   slots** (16,844 MiB — down from 12,920 / 29,748 MiB on 4 GPUs), **prompt chunk 6,400** (was 8,192).
   Decode is visibly slower: the CPU expert time per window rose to **17–27 ms** (was 2.5 ms on 4 GPUs
   with the KV streamed) and recent decode rates are **7.6–13.7 tok/s**.

**Lesson:** the profile's `layer_split` length is coupled to the **live** GPU count. A card disappearing
from the bus does not change the profile, so the engine refuses to start. When a card is lost, the
profile must be re-cut for the surviving cards (or `layer_split` set to `auto`) and the 4-GPU file kept.

**Also:** `nvidia-smi -r -i 3` cannot reset a card that has fallen off the bus ("Unable to determine
the device handle"), and `rmmod nvidia_modeset` needs `-f` while `nvidia_drm` holds it. The remaining
fix is physical: reseat the M.2_2 riser / the **`00:1d.7`** cable, then restore `strata-iq4xs-4gpu.json`
and restart — a reboot is also acceptable (the modules are reloaded at boot).

**The overnight watchdog, v2.** The v1 watchdog exited early ("batch finished on its own") during the
GPU3 outage, so the batch was left unsupervised for 20 minutes. v2 (`/tmp/opencode/watchdog.sh`) checks
the batch FIRST and only exits when the batch is genuinely gone; it needs **3 consecutive** failed
`/health` probes (8 s each), PSI memory avg10 **> 40** (not 25 — the engine's own reclaim pushes PSI
to 15–19 transiently on a 3-GPU box), or MemAvailable < 2 GiB.

**Independent agreement with Boxcat.** Boxcat separately read the live profile and reached the same
conclusion on the eviction cause (`--conversation-cache-slots 16`, `live parked=4`, `bytes=4.4 GiB` of
a 256 GiB budget) and independently reported the same RAM attribution (strata-host 79.3 GiB, Hermes
3.2, Hindsight 1.8). It also quoted a **stale shape** (`--kv-resident 32768`,
`--conversation-cache-mib 40960`) — the live values are `--kv-resident 20480`, `--kv int8`,
`--conversation-cache-mib 262144`, `--conversation-cache-slots 16`, `--adapt-min-gain 6`. Boxcat was
briefed on all of it (four mailbox messages, seq 36–39).

## 2026-11-21 04:15 → 05:00 — upstream v0.23.3 merged, 128 cache slots, and the warm-engine proof

**The upstream update (v0.22.2 → v0.23.3, 20,603 commits) is merged and live.** HEAD `33cbdad2ef`,
`code_version 0.23.3` per the gateway's own control socket, 18 profiles served, both platforms connected.
The merge was done in a **scratch worktree first** (`/tmp/opencode/wt-ours`), never on the live tree.

**24 conflicts, resolved by hand:**
- 16 were the files we had locally **deleted** (`agent/lsp/*` 13, `plugins/google_meet/node/*` 3, their tests) — they stay deleted; upstream re-added them.
- `gateway/platforms/api_server.py`: upstream's **config-guard middleware stays outermost**, Clara's access-log middleware kept alongside it; upstream dropped `import sqlite3` (now unused), Clara's `import subprocess` kept.
- `plugins/platforms/line/adapter.py`: **upstream's version taken** — Clara's fork is 2,159 lines ahead of upstream, so upstream's 17-line change (the proxy-URL redaction + the request-id-as-capability check) was already absorbed. Keeping upstream's is what keeps the LINE ingress working.
- `tools/browser_use_cli.py`: upstream's taken, then **Clara's two changes re-applied** into upstream's new structure — the headless/WSL CDP probe (127.0.0.1:9222) and the Playwright `BH_CHROME_PATH` hint.
- `tools/computer_use/cua_backend.py`: **both** — Clara's WSL `.exe` `snapshot_id` path and upstream's token-only schema handling.
- `hermes_cli/kanban_db_dispatch.py`: upstream's `kanban_worker_client` spawn (the dispatcher now dials the profile's gateway), Clara's `HERMES_KANBAN_IGNORE_MEMORY_PRESSURE` override kept, and the retry-with-preserved-workspace prompt **re-applied** on the new path in `gateway/session_kanban.py` (as a module-level helper `_worker_task_prompt`).
- `agent/codex_runtime.py`: **both** — Clara's `_clara_codex_worker_cwd` + `_block_kanban_after_codex_failure`, upstream's `_redact_projected_assistant_row`.
- `apps/desktop/src/styles.css`: upstream's restyle then Clara's HUD block; the seam's closing brace had to be restored by hand (git's conflict had eaten it).
- `AGENTS.md`, `tests/hermes_cli/test_fallback_config.py`: both sides kept. `package-lock.json`: **deleted** — upstream migrated to pnpm.

**The services needed a runtime change.** v0.23.3's pins are `python_version >= '3.14'`, but the repo venv is **Python 3.11**, so it resolved to pydantic 2.13.4 while the code imported `pydantic_core.ArgsKwargs` (3.14-only) — the gateway crash-looped with `ImportError: cannot import name 'ArgsKwargs'`. **Fix:** the services now run through the tree launcher `/home/clara/.hermes/hermes-agent/.hermes/bin/hermes` (which execs the store python 3.14.9) instead of the hardcoded 3.11 `venv/bin/python`:
`/etc/systemd/system/hermes-{gateway,dashboard}.service.d/90-runtime-v231.conf` (unit files backed up as `*.service.bak-20261121-v231`). **0 errors and 0 restarts since 04:52:51.**

**The conversation cache: 16 → 128 slots.** The only eviction trigger is `entries_.size() >= slots_`
(`include/strata/core/conversation_cache.hpp:395`) — the **slot ceiling**, never the byte budget and never
the RAM gate (there is not a single *"evicted N parked conversations"* line in the log; that is the RAM
gate's message). 1,439 distinct live conversations compete for the slots, so 16 was always too few:
**107 evictions** and a 60 % reuse rate (314k of 781k tokens re-read). At 128 slots: **evictions = 0**,
parks 300–3,300 ms, and the surplus pages to the NVMe swap (`STRATA_PARK_SWAP_GIB=256`, 251 GiB free).
Note the slot count is a **ceiling, not a RAM budget**: the RAM side is the cgroup high (80 GiB) and
`--conversation-cache-mib 262144` (256 GiB logical).

**The warm-engine proof (the owner's goal: "it feels fast when I retry when engine is warm").**
Measured on the Boxcat chat (62k-token context) through the real Hermes path:
| | prompt | reused | read | TTFT |
|---|---|---|---|---|
| first turn (cold) | 62,386 | 0 | 62,386 | **94.2 s** |
| retry 1 (warm) | 62,531 | 62,513 | 18 | **0.8 s** |
| retry 2 (warm) | 62,484 | 62,466 | 18 | **0.8 s** |

So the **cold** cost is a 94-second prefill and the **warm** cost is 0.8 s. The cache is what makes
the difference, and it is only kept if the slot ceiling is high enough — that is the whole reason the
evictions mattered. End-to-end wall time for a warm continuation through Hermes: **5.5–6.5 s**.

**Everything is local now.** 849 session rows were repointed from cloud models to the local engine;
`advisor` and `codexworker` (both dormant) now default to the local engine; **`codex-worker` keeps
`gpt-6.1-sol` / `openai-codex`** — the owner's stated exception. Every profile's
`auxiliary.compression` is `strata / qwen3.8-flash-next-iq4_xs`.

### The v0.23.3 API moves (the tools had to follow)

- **`hermes_state.SessionDB` is deprecated** (removed in v0.27.0): it no longer has `_read_ctx`,
  `update_session_model` or `patch_session_config`. The new API is
  `persistence_assembly.open_persistence(profile_scope(home=...))` → `p.sessions.update_model(...)`,
  `p.messages`, `p.coordination`, … For a *read*, plain SQLite (`sqlite3.connect('file:...?mode=ro')`)
  still works and is version-independent — use it.
- **The compression lock now needs the persistence handle.** `agent._session_db` must be the
  persistence handle (v0.23+) or the `SessionDB` (v0.22-); with neither, `compress_now()` returns
  `lock_skipped` ("the lock check failed") instead of compressing. The tool now passes
  `persistence=` when the handle exists.
- **The runtime must be 3.14.** The repo `venv/` is Python 3.11 and cannot satisfy v0.23.3's pins
  (`python_version >= '3.14'`), so the engine's own `--serve` path is fine but anything importing
  fastapi/pydantic needs the store python. `run_compress.sh` now uses the 3.14 store python with the
  managed install's `site-packages` on `sys.path` (the tree launcher's own shape).
- **Stale locks accumulate when a process dies mid-compression.** `compression_locks` and
  `session_turn_leases` keep the dead PID; the lock check then refuses every later attempt
  (`lock_skipped`). Clear the rows whose holder PID is dead or whose `expires_at` has passed.

### Final state (2026-11-21 05:15 UTC)

| | |
|---|---|
| Hermes | **0.23.3** (sha `33cbdad2ef`), 18 profiles, api_server + hermex connected, **0 restarts** since 04:52:51 |
| Engine | UD-IQ4_XS on **3 GPUs**, 7,244 expert slots, chunk 6,400 |
| Conversation cache | 128 slots, **evictions 0**, 14 parked, 17.9 GiB of a 256 GiB budget |
| Memory | MemAvailable 13.8 GiB, PSI some avg10 **0.00**, swap 18 of 278 GiB |
| Health | engine 22 pass / 0 fail · Hermes 20 pass / 1 warn (a cron ticker age) / 0 fail |
| Bot | warm continuation **1.6 s TTFT**, 8–11 s end-to-end through Hermes |
| Batch | summariser running on the remaining 12 bloated chats (15 of 27 done) |

**The GPU3 card at PCI 0000:08:00.0 is still dead** (Xid 79, fallen off the bus) and needs the physical
riser reseat; until then the engine runs on 3 cards at roughly half the decode throughput.

## 2026-11-21 05:20 — everything pushed; the owner is resetting the box

**The owner decided to stop using local inference** and to reset the machine, so everything was pushed
before the reset:

| what | where |
|---|---|
| the whole Hermes tree (v0.23.3 merge + the Clara customizations) | `github.com/tataetae/hermes-agent` branch **`clara/v0.23.3-merge`** (HEAD `33cbdad2ef`); a fork of NousResearch/hermes-agent was created because the upstream denies write access to this account |
| the session record, the tooling, the engine profiles, the systemd drop-ins, the Hindsight env | `github.com/tataetae/Strata` branch **`clara/hermes-state`** (HEAD `6cdbfec92`), under `docs/hermes/` — `README.md` has the rebuild order, `AGENTS.md` the full record, `tools/` the scripts (they lived in `/tmp/opencode` and would have been wiped) |
| the pre-update backup (2.0 GB of `state.db` + configs) | `/home/clara/backup/hermes-pre-update-20261121T0417` — **local only, it did not survive the push** (too big for git) |

No secrets were committed: the engine key is read from the Hermes config at run time and the Hindsight
env's key is a placeholder.

**What the reset will undo (the README's rebuild order):** the tree's own git remote is the read-only
upstream, so `git pull` alone does NOT bring the merge back — the branch must be fetched from the fork;
the engine profile (`docs/hermes/strata-iq4xs-3gpu-live.json`), the systemd drop-ins (without them the
gateway crash-loops on `ArgsKwargs`), the Hindsight env, and every profile's model route all need
restoring.

**On the surprise that 4 GPUs ran and 3 GPUs also ran:** the split was never the limit — the *card count*
is only what `layer_split` has to match. 4 GPUs gave 12,920 expert slots / chunk 8,192 / 2.5 ms CPU-expert
per window; 3 GPUs give 7,244 slots / chunk 6,400 / 17–27 ms. The engine's own `auto` search places the
stages by free VRAM, so it re-cut itself (K=13,32) and ran correctly on 3 cards. The missing card costs
roughly **half the decode throughput** (7.6–13.7 tok/s vs 20–40) — that is the one real price of the dead
GPU, and the reason the owner stopped using local inference.

## OPEN: Thai text generation leaks wrong tokens (owner-reported 2026-11-20)

**Symptom** (reported by the Boxcat/system-maintainer agent's own Thai test, 609 chars):
the Thai output is NFC-stable and has **0 U+FFFD replacement characters** — so the *encoding* and
composition layers are fine — but the **generation** stage leaks tokens that do not belong:
- Chinese characters in the middle of a Thai sentence: `等不同` (U+7B49 U+4E0D U+540C)
- an English fragment at the end of a line: `but no need.`
- another case: an English word glued in (`_agent`), split wrongly
The same agent reports an earlier, much worse symptom of the same family (a phrase repeated
hundreds of lines) on 2026-11-19 evening.

**Working hypotheses (none confirmed yet):**
1. **Speculative decoding.** `--spec 8` is live. The engine's own docs say that with the **IQ models**
   "the CPU rounds one-token expert groups and multi-token groups differently, so the same prompt at
   temperature 0 can end in a different, equally good answer when drafting, cache state or a resumed
   conversation differ" — i.e. on an IQ build the *verified* token can depend on whether it was drafted.
   `STRATA_IQ_MT_MIN=1` is the documented fix ("the answer no longer depends on drafting", cost: IQ decode
   −1 to −3 %). Our build is an IQ build (**UD-IQ4_XS**), so this path is live.
2. **Thai is byte-level BPE**, so a Thai character is ~3 tokens: multi-token expert groups are far more
   common in Thai than in English, which is exactly the path the IQ rounding note is about.
3. The draft head's vocabulary subset: `mt/rt/draft_vocab.bin` holds 106,299 of 248,320 ids (43 %).
   85 % of the Thai-capable byte tokens are inside it, **12,052 are not** — the drafter cannot propose
   those, which lowers the acceptance rate on Thai text (not a correctness bug on its own).
   `tools/draft_vocab.py` can build a subset; `--mtp-draft-vocab FILE` uses your own.

**Next step when this is picked up (Thai is a second priority for the owner):**
- Run the same Thai prompt at temperature 0 with `--spec 8` vs `--spec 1` and compare the text: if the
  speculative run contains CJK/English that the plain run does not, speculation is the cause.
- Then `STRATA_IQ_MT_MIN=1` (correctness first, ~1-3 % decode) and re-check.
- Then consider a Thai-inclusive draft subset for the acceptance rate.

## 2026-11-19 10:00 → 11:00 — IQ4_XS + 4 slots + the NVMe question (opencode session)

**Live: `UD-IQ4_XS`** (profile `strata-iq4xs.json`, 3 slots). The XXS build was **deleted** (shards + pack + profile).

| arm | build / shape | slots | prefill 16k (wall) | decode, 3 clients |
|---|---|---|---|---|
| M0/M3 | `UD-Q4_K_XL`, 3 slots | 6,449 | 869.8 s | 20.3–36.9 |
| M4 | `GSQ-RCO-IQ3_XXS`, 3 slots | 13,080 | 86.5 s | 68.9 — *owner rejected: hallucinates* |
| M5 | `UD-Q3_K_XL`, 3 slots | 9,242 | **122.9 s** | **54.9** |
| M6 | `UD-IQ4_XS`, **4 slots** | 7,227 | **695.4 s** | 43.0 |
| M7 | `UD-IQ4_XS`, 3 slots + NVMe tuning | 8,758 | 144.2 s | 51.3 |

- **4 slots is a loss on this build** (the owner's request was measured, not assumed): the cold prefill goes
  122.9 s → 695.4 s (the chunk cap collapses 5,632 → 2,816 because each slot's K/V comes out of the expert cache)
  and the decode aggregate drops 54.9 → 43.0. Reverted to 3 slots; flip `--parallel`/`--batch-groups` to 4 to restore it.
- **Quality ladder of the experts** (weighted average bits over the expert blobs): Q3_K_XL **4.12 bpw** →
  IQ4_XS **4.36** → Q4_K_XL **5.06**. IQ4_XS is ~6 % finer than Q3_K_XL for ~7 % less decode and ~17 % more prefill
  time; `UD-IQ4_XS` is the smallest build in the Q4 class.
- **Resizable BAR is ALREADY ON** — `BAR 1: current size: 16GB` on all four cards (it would be 256 MB with rebar
  off). **Nothing to do at the BIOS**; the host↔device upside is already in hand.
- **The NVMe/file tier is INACTIVE for these builds** — the arena (capped at the model's expert size: 43.7 GiB for
  IQ4_XS) holds the experts in RAM, so no expert read ever touches the disk (the engine prints no file-tier line).
  The NVMe only serves the startup load. The tuning applied anyway (it matters the moment the tier activates, e.g.
  after an eviction or a VRAM resize): `STRATA_SSD_GBPS=5.9` (**the engine assumes an NVMe is 2.5 GB/s**; ours
  measures 5.9, so the prompt planner was pricing the drive at 42 % of its real speed), `STRATA_IO_PREFETCH=1`
  (it self-disables when the page cache has no room — our case), `STRATA_IO_PF_THREADS=12`, `STRATA_FILL_AHEAD=1024`,
  `STRATA_LOOKAHEAD_K=16`. **Measured effect on this build: none** (prefill 144.2 s vs 122.9 s for Q3_K_XL is the
  build difference, not the tuning).
- **Page size:** the *file* side cannot be changed on this kernel — `CONFIG_READ_ONLY_THP_FOR_FS is not set`, so
  `/srv/inference` has no `huge=` option and the model's page cache stays 4 KiB pages. The anonymous side is
  **THP=always** (see below); the engine already madvises its arena, and `defrag=madvise` keeps compaction off the
  CPU-bound expert path.
- **Hugepages: THP=always is ON** (`/etc/systemd/system/thp-always.service`, enabled; the active value is in
  brackets in `/sys/kernel/mm/transparent_hugepage/enabled`). The engine already madvised its arena; this extends
  the same 2 MiB pages to the conversation-cache snapshots it copies on every park. Measured like-for-like (same
  build, same 3 slots, fresh engine both times): **cold prefill 144.2 s -> 129.6 s (-10 %)** and **park time
  125.8 ms -> 68.7 ms (-45 %)**. `defrag` stays `madvise`, so only madvised regions ever compact synchronously and
  the CPU (the expert path) is not stalled; `thp_fault_fallback` is 0.
  **The explicit MAP_HUGETLB pool stays OFF** (`vm.nr_hugepages=0`): it needs 28,796 pages (56 GiB) all-or-nothing,
  it starved parking and measured 241 vs 330 tok/s. Re-arming it would be the aggression to avoid.
- **Aggressiveness audit (2026-11-19 11:00):** engine cgroup high 84 GiB / max 112 GiB / current 84 GiB (the
  difference is the reclaimable page cache of the 87 GiB of model files, not the engine's own memory); arena pin 70
  (capped by the model: 43.7 GiB of experts, all of it pinned in RAM); conv cache 40,960 MiB with floor 0 (the cgroup
  high is the governor); `vm.swappiness` 60 (the owner's); MemAvailable **30 GiB**, swap 6.3 of 112 GiB, **PSI 0.00**.
  The NVMe knobs force the io prefetch on with 12 threads, but the **gate is still on**, so it cannot thrash the disk.
- **The real lever for the cold read is still RAM bandwidth**: a 50,945-token fresh read now runs at **451 tok/s**
  (engine-reported) on the IQ4_XS, against ~330 tok/s on the Q4 — the smaller expert blobs and the bigger resident
  set did it, not the disk.







## Hermes
- DB `/home/clara/.hermes/state.db` (authoritative). Providers: `strata` is the default for every profile
  except `codexworker` (luna), `misc` (ollama-cloud), `plc` (custom).
- Services (all `Restart=always`): gateway `8647`, **WebUI `8788`** (loopback; nginx on `8787` in front of
  it, and the `.80` relay reaches it through tunnel `18788`), dashboard `9119` (basic auth `clara`),
  **dashboard also proxied on `:2000`** (nginx `hermes-direct-2000`), health API `9120`, Hindsight `8888`,
  Strata `8080`, **priority gate `8082`**, FreeCAD noVNC `6080`.
- **Queue priority:** chat talks to Strata directly; Hindsight's LLM goes through `strata-priority-gate`
  on `:8082`, which holds its requests while any chat request is in flight. Hindsight *recall* never
  queues — it never touches the engine.
- Hindsight: `local_embedded`, bank `hermes`, LLM = local Strata via gate `:8082`, embeddings on CPU.
  Caps live in `/etc/hermes-hindsight.env`. Completion budgets: consolidation **16384**, reflect **16384**
  (the old 1024 cap silently killed consolidation AND reflect final answers — signature:
  `answering: N of max 1024 … (length)` → `structured_output_failed` / HTTP 500). Reflect wall **3600 s**,
  plugin client timeout 3700 s. `LLM_REASONING_EFFORT=none`. Consolidation runs as a long-lived worker
  op that survives restarts.
- **Hindsight tool cost (warn agents):** `hindsight_reflect` is an agentic loop — up to 10 LLM passes,
  ~600–900 s on a loaded engine (wall 3600 s). Use sparingly, cross-session synthesis only;
  `hindsight_recall` (fast DB query) is the right tool for ordinary lookups. `hindsight_retain` returns
  immediately. Consolidation is background maintenance (~30–90 s per memory), never agent-blocking.
- Context budget is deliberate: `model.context_length` **262,144**, `compression.threshold_tokens`
  **150,000** (owner, 2026-11-19: down from 204,800 — the cron fleet's long conversations held the engine's
  slots for 500–1000 s each, which is what the owner was waiting on), `target_ratio 0.45`, `protect_last_n 20`,
  **`micro_compact: false`** (it rewrites the prefix and kills the provider cache), `memory.flush_min_turns 15`.
- **Pre-compaction probe — compact at a break, never mid-sequence.** Compaction sends the whole
  conversation to the summarizer (a ~205k prefill = minutes here) and the next turn is cold. Before a
  long tool sequence, check `/context`; past ~⅔ of `threshold_tokens` (~135k), run `/compress` there and
  then, at a natural break. Idle sessions compact on their own after 30 min. Subagents do the same at
  120,000.
- WebUI session quirks (404 on gateway sessions / 403 profile binding) and "no Hindsight web UI" are in
  the Hindsight bank — recall, don't re-debug.

## Access
- OpenCode provider `strata/qwen3.8-flash-next-iq4_xs`. FreeCAD 1.1.4 streamed at `http://192.168.1.36:6080/vnc.html` (pw `clara-freecad-2026`).
- UFW allows 22, 6080, 8080, 8647, 8787, 9119, 9120. `8788` is loopback-only by design — reach the WebUI on
  `8787` (nginx) or through the `.80` relay, never by opening 8788.

## 2026-11-21 08:00 → 09:20 — the sweep winner, the false-success fix, and the p14 lane wakes up

**The engine shape sweep (6 arms, 4 GPUs back).** Each arm was a full engine restart plus a 3–4 client
cold-prefill + decode run (`/tmp/opencode/arms.py`, results in `/tmp/opencode/sweep_results.jsonl`).
Absolute rates are depressed by the Boxcat bot's own traffic, but the arms are like-for-like.

| arm | wall s | prefill tok/s | decode tok/s | evictions | reused tok |
|---|---|---|---|---|---|
| A0 baseline (kv 20480, 3 slots) | 634.6 | 455.5 / 423.0 / 414.3 | — | 0 | 731 k |
| A1 2 slots | 696.2 | 408.0 / 405.4 / 383.3 | — | 0 | 937 k |
| A2 4 slots, kv 20480 | 708.0 | 539.6 / 484.9 / 450.8 | 15.2 / 10.8 / 8.6 / 8.2 | 0 | 1.28 M |
| A3 3 slots, kv 32768 | 686.1 | 503.4 / 476.3 / 466.7 | 15.1 / 12.9 / 11.1 / 10.8 | 0 | 1.74 M |
| **B0 3 slots, kv 32768** | **555.8** | **503.5 / 500.2 / 487.8** | **46.3 / 35.2 / 24.1 / 18.7** | **0** | **2.20 M** |
| B1 4 slots, kv 32768 | 598.8 | 546.6 / 540.1 / 489.8 | 47.5 / 38.1 / 15.3 / 14.1 | 0 | 2.24 M |

**Winner = B0: `--parallel 3 --batch-groups 3 --kv-resident 32768`** (applied 08:36, live: 11,096 expert
slots, chunk 6,912, split `0-8/9-21/22-35/36-47`, `layer_split: auto`). Four slots buy a 4th resident
session but cost the per-stream rate (the 4th stream runs at half speed). `--kv-resident 32768` is the
single biggest lever: it moves the K/V to pinned RAM and gives the freed VRAM to the expert cache.

**The summariser's FALSE SUCCESSES — Boxcat caught it, and it was right.** The batch had reported
"compressed in 523s" for **13 sessions that had `compacted=0` rows**. `result.status` only says the
summariser RAN; only the DB says the archive LANDED. `fire_compress.py` now does a **COMMIT CHECK**
(`compacted_rows` + rotated children) and returns `false_success` when nothing was archived; the batch
runner counts that as a failure. Verified on the session Boxcat named: `a23fd9b5de72` 507,756 → 125,017
tok, 833 → 299 messages, 542 rows archived (DB now reads active=299 / compacted=542). Rebuilt the batch
list from the DBs: **24 live chats over 150k tokens, 11 genuinely compacted, 13 false**.

**The watchdog is now v3 (Boxcat's stricter rule).** `/tmp/opencode/watchdog.sh` stops the batch only when
`/health` fails **3 consecutive times AND the engine's systemd unit is inactive** (the engine really died),
or when PSI memory avg10 > 40, or MemAvailable < 2 GiB. v1 stopped the batch on a SINGLE timeout while
the engine was healthy; `/health` answers in 0.00–0.02 s here even mid-prefill (measured, 8 probes).

**The p14 software lane woke up.** It was not the dispatcher: the dispatcher runs in the **gateway**
(`kanban.dispatch_in_gateway: true`), holds the singleton lock (`~/.hermes/kanban/.dispatcher.lock`, PID
1685), ticks every 60 s, and `dispatch_profiles` is **absent from the config → "any"** (`hermes kanban
diagnostics` says so). The lane was idle because the board held **41 blocked / 2 done and ZERO ready**
cards. The only card whose documented gate was a *resolved* condition was **`t_7cdf55f7` (ledger B8)**,
blocked by "the Strata engine is saturated" — unblocked it (the owner's authorisation to restart the
engine and continue the p14 work), and the dispatcher claimed → spawned (`pid 113745`) → the **software
profile's owner admitted the claim** (`owner_admitted`, session `local-f1cc32cb…`) → the agent is running
the card. The card's workspace is a **scratch dir** (`…/boards/p14/workspaces/t_7cdf55f7`), `project_id`
and `branch_name` are NULL, so the agent must find the code itself — the p14 clones live in
`~/workspace/p14-handoff-20261101/{p14-backend,p14-frontend,p14-plc}` (the software profile's MEMORY.md
says so). **`/home/p14/repo` does NOT exist on this box** — the card bodies still point at it.
**Still blocked for the owner, not for me:** 5 HUMAN cards (A1–A4, E1, E2, C1–C5, F3, D1, D3, E6 — the
site/physical ones), plus the PLC cards (a real-PLC download needs authority that
`SITE-DEPLOYMENT-AUTHORIZATION-20261112.md` withholds).

**Do NOT merge the 334 unreleased `origin/main` commits.** v0.23.3 is still the newest release; of the 334,
**0** touch the compression core (`agent/`), **0** touch the session store, and only 2 touch the kanban
path (both cosmetic "Kanban wake" UX fixes). The v0.23.3 merge cost 24 hand-resolved conflicts; there is
no defect here that it would fix.

**PCI topology — corrected (verified from `lspci -D -t` + `nvidia-smi`):**

| GPU | PCI | root port | link | device id |
|---|---|---|---|---|
| 0 (main) | `01:00.0` | `00:01.0` | x16 | `10de:2503` |
| 1 | `03:00.0` | **`00:1a.0`** | x4 | `10de:2504` (**Lite Hash Rate**) |
| 2 | `06:00.0` | `00:1c.4` | x4 | `10de:2503` |
| 3 | `08:00.0` | **`00:1d.7`** | **x1** (M.2_2 riser) | `10de:2503` |

The card that fell off the bus (Xid 79) was **GPU3 on `00:1d.7`**, not `00:1a.0`. After the reseat +
reboot: `00:1d.7` AER counters read **0**, `00:1a.0` reads **1 RxErr** — GPU1's link is still the
marginal one, and the 10,935-vs-9,247 "AER" figures in the earlier notes are **two different
instruments** (a journal-line count vs the sysfs `aer_dev_correctable` sums) — never trend them together.

**Pushed:** `tataetae/Strata` branch `clara/hermes-state` → `bec86ad87` (the tools with the COMMIT CHECK,
the live 4-GPU profile, the systemd drop-ins, the notes). `tataetae/hermes-agent` `clara/v0.23.3-merge`
is unchanged.

## 2026-11-21 09:30 → 09:50 — the dashboards: two boot-time regressions and a noisy alert

The owner asked to check the dashboard and update them too. The **Hermes dashboard is healthy**
(`/api/status`: v0.23.3, gateway running, api_server + hermex connected, 18 profiles, 4 active agents),
but the health sheets underneath it were reporting real regressions that the reboot had caused.

**1. THP was `never` — a boot-time race between two units.** `health_check.sh` WARNed "THP = never".
Cause: **`thp-always.service` (created 2026-11-19) and `crash-mitigate.service` (created 2026-11-11,
"THP off + zswap off — kernel mm corruption test") both write
`/sys/kernel/mm/transparent_hugepage/enabled`, and neither ordered itself against the other.** At the
06:33 boot, `crash-mitigate` ran at 06:33:30 and `thp-always` at 06:33:29 — so the mitigation won by one
second and THP stayed `never`. That costs the engine the measured **−10 % cold prefill and −45 % park
time** (AGENTS.md, 2026-11-19). Fixed: `thp-always.service` now carries `After=crash-mitigate.service`
(backup `thp-always.service.bak-20261121T0940`), and THP is `always` again.
**The mitigation never worked anyway:** the crashes it was chasing are 2026-11-14 06:08 (diagnosed as
the ICU/locale problem, not mm corruption) and 2026-11-17 14:35 — and 11-14/11-17 both happened *with*
THP off.

**2. Swap was 112 GiB, not 278 GiB — `swapfile4` was never re-enabled.** `/srv/inference/swap/swapfile4`
(160 GiB) exists on disk but was only `swapon`ned by hand, so the reboot dropped it: the kernel saw
`SwapTotal = 114,687 MiB` while the notes said 278 GiB. That matters because the engine's parking
admission counts free swap as headroom (`STRATA_PARK_SWAP_GIB=256`). Fixed: `swapon` + a
`/etc/fstab` line (backup `fstab.bak-20261121T0940-swapfile4`) → **278,527 MiB, 251 GiB free**.

**3. The health API alerted on a hot GPU with no throttle behind it.** It fired "GPU 0 temperature is
high at 80 C" and flipped the whole host to `degraded` — but 80 C is **normal** for an RTX 3060 under a
200k-token prefill, and `nvidia-smi -q -d PERFORMANCE` reported **no active throttle reason** at all.
The rule now asks for `clocks_event_reasons.sw_thermal_slowdown` and alerts at **85 C, or at 80 C only
when the card is actually throttling** (`hermes_health_api_wsl.py`, backup
`*.bak-pre-gpu-thermal-rule-20261121T0945`; the field is exposed as `gpu.devices[].thermal_slowdown`).

**4. `HEALTH-CHECKLIST.md` was rewritten** — it was the 11-19 sheet and every number in it was stale:
the expert cache (19,370 MiB / 6,450 slots → **25,842 MiB / 11,096**), the prompt chunk (6144 → **6912**),
the log file (`strata-iq3_xxs.log` → **`strata-iq4xs.log`**), the cgroup (`high 100 / max 112` →
**80 / 136 GiB**), the decode threshold (CPU < 30 ms/window → **< 6 ms**, because `--kv-resident` moved
the K/V to RAM), and the dispatcher ("runs in hermes-dashboard" → it runs in the **gateway** since
v0.23.3). New rows: THP (3.7), the swap files (3.8), the dispatcher's allowlist (4.7), the dashboard's
own API (4.8), the Hindsight model name (5.5) and the batch's commit check (6.5).

**Watch item: CUDA0's VRAM.** The main card carries the head + the MTP drafters, so it has the least
headroom — **440 MiB free today** (GPU1 893, GPU2 625, GPU3 651). The engine reports
`vram_free_mib 1038` and the log shows **"--vram-elastic is off: a layer split has a cache per GPU"**, so
the reserve cannot be given back dynamically. `--vram-reserve-mib 1024` is the floor (512 produced
`cuBLAS status 14` at 19 MiB free on CUDA3). Below ~100 MiB free on CUDA0 the next thing to try is
`--kv-resident 20480` (frees the K/V ring's VRAM into expert slots).

**Both health sheets now pass:** `health_check.sh` **22 pass / 0 warn / 0 fail**,
`hermes_health_check.sh` **20 pass / 0 warn / 0 fail**.

## 2026-11-21 09:55 — OWNER: "stop all background tasks" (the engine is reserved for the owner's chat)

The owner was engaging with **@hermes (root)** and **@system-maintainer (Boxcat)** and asked to stop
everything background. **What was hammering the engine:** the summariser batch (each compaction is a
~50k-token prefill on a 240k conversation), 16 enabled cron jobs, 2 p14 kanban workers, and the root
bot's own "W1" workstream. Everything below is **stopped and reversible**:

| what | how it was stopped | how to resume |
|---|---|---|
| the summariser batch (`batch_run2.py` + its child + the watchdog) | killed (PID 110310/110311/148299/115088) | `python /tmp/opencode/batch_run2.py` — it **skips anything that already has a compacted row**, so nothing is repeated. It stopped at **8/24** |
| **every cron in every profile: 39 jobs, 0 enabled** | `hermes cron pause <id>` per job (root 8, system-maintainer 8, cad 1) | `hermes cron resume <id>` (with `HERMES_HOME=<profile home>`) |
| the p14 cards `t_7cdf55f7` (B8) + `t_6db82a96` (F8) | `hermes kanban reclaim` → `hermes kanban block` → they landed in **triage** ("unblock loop detected"), so the dispatcher has nothing to spawn | `hermes kanban unblock <id>` — the dispatcher re-runs them within 60 s |
| the only dispatchable card on any board (`t_424bb60a`, h2o2 SPA bind) | `hermes kanban --board h2o2 schedule` — **a `todo` card cannot be `blocked`, only `scheduled`**, and Scheduled cards are not dispatched | `hermes kanban --board h2o2 unschedule <id>` (or block with `--kind needs_input`) |
| the root bot's W1 workstream | the "stop all background work" message was delivered to the root bot (`runtime/bot_live_delivery`, `status=queued` → claimed at its next turn boundary); W1 was a read-only check and finished on its own | — |

**Do NOT use `hermes pause` for this.** It halts **new gateway turns** too, so the owner's own chat with
@hermes and @system-maintainer would come back "Hermes is paused" — the opposite of what they wanted.

**Why the dispatcher is safe to leave running:** `kanban.dispatch_profiles` is absent (= "any"), the
dispatcher ticks every 60 s in the **gateway**, and it only spawns cards in `ready`/`todo`/`running`.
With 0 dispatchable cards it has nothing to do. A card that flips back to `todo` on its own (e.g. a
dependency re-opening) would spawn a worker again — that is the one thing to watch.

**Final state:** engine `queued=0`, **0 requests in 180 s**, 3 slots free for the owner's chat,
conversation cache parked 23 / **evictions 0** / 3.99 M tokens reused.

## 2026-11-21 11:10 → 11:30 — "have we updated the engine service itself?" → four stale references, and the auto-decomposer

**The engine service WAS updated** (the live `ExecStart` is `--profile strata-iq4xs.json --parallel 3`,
the cgroup carries MemoryHigh 80 GiB / MemoryLow 72 GiB / MemoryMax 136 GiB, the `STRATA_*` env has
every tuning knob, and the profile itself has the staged flags: `--kv int8`,
`--mtp-draft-vocab .../draft_vocab_thai.bin`, `--kv-resident 32768`, `--conversation-cache-slots 128`,
`--adapt-min-gain 6`). **But four references inside the service's own plumbing were stale:**

1. **The base unit's `ExecStart` still named the DELETED profile** `strata-iq3_xxs.json` with
   `--parallel 1` (the drop-ins masked it — lose the drop-in and the engine crash-loops on a missing
   file). The base unit now carries the live shape, and the Description reads
   "Strata UD-IQ4_XS inference server (4 GPUs, 3 slots, K/V in RAM)" instead of "IQ3_S".
   (backup `strata-iq3s-server.service.bak-20261121T1115`)
2. **The profile's `parallel` said 1** while the service runs 3, and **`pack` still pointed at
   `packs/q4_k_xl`** while the engine serves `packs/iq4_xs`. Both are metadata (the CLI args win at
   runtime) but the **pushed copy carried them** — a rebuild from git would have produced **one slot
   instead of three**. Both are now correct in `strata-iq4xs.json` and in
   `docs/hermes/strata-iq4xs-live.json`.
3. **Three scripts read a dead engine log.** `hermes_health_api_wsl.py` pointed at the **deleted**
   `strata-iq3_xxs.log`; `strata_task_logger.py` and `stall-watch.py` pointed at the **frozen**
   `strata-iq3_s.log`. So the dashboard's engine block and the logger's counters were reading a file
   the engine stopped writing 3 days ago. All three now follow `strata-iq4xs.log` — the health API's
   engine block now reports the live `model qwen3.8-flash-next-iq4_xs / kv int8 / expert_cache 25,842
   MiB / batch 3+3`, and the task logger follows the live log again.
4. The health API's **quant map did not know `iq4_xs`** (it was right only by the upper-case
   fallback). It is explicit now, with `iq4_nl / q4_k_xl / q3_k_xl` alongside the IQ ones.

**The auto-decomposer was the last background writer.** Unblocking the ledger **F8** card made the
dispatcher **split it into three children** (`t_9eefb4f6` reproduce+triage, `t_3accdc53` fix to zero
errors, `t_7fdb7777` qc verify) — so parking the parent was not enough: the children had to be parked
too, and the parent was then **promoted back to `todo`** by `recompute_ready` and spawned again.
**`kanban.auto_decompose: false` is now in `/home/clara/.hermes/config.yaml`** (the toggle is re-read
every dispatch tick, so it needs no restart). Set it back to `true` when the p14 lane resumes — but
expect F8 to fan out again.

**The trap to remember: a `todo` card cannot be `blocked`** — `block_task` only transitions
`running`/`ready`. A `todo` card must be **`schedule`d** (Scheduled cards are not dispatched) or given
`--kind needs_input` on a `blocked` card. Every card I parked this way is Scheduled or in triage.

**Final state:** 0 dispatchable cards on every board, 0 kanban workers, 0 cron jobs enabled (39/39
paused), the summariser batch and the watchdog stopped, engine **queued=0, 0 requests in 60 s**.

## Rules
- Old disks stay read-only. Never expose 8647/9119 beyond the LAN without auth.
- Don't send external messages on the owner's behalf. Don't delete the only copy of anything.
- Never write to `clara-serv-01` as `deploy`; Nextcloud is the file interface.
