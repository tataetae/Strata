# clara-ai-server — health checklist

Run the automated version first: `bash /home/clara/logs/health_check.sh` (read-only,
exit 1 on any FAIL). It covers everything below; this sheet is the human version,
with the thresholds and what each signal actually means.

**Last run: 2026-11-21 09:45 UTC** — 22 pass / 0 warn / 0 fail (engine),
20 pass / 0 warn / 0 fail (Hermes).

Live shape on this date: **UD-IQ4_XS on 4 GPUs**, `--parallel 3 --batch-groups 3
--kv-resident 32768`, `layer_split: auto`, 11,096 expert slots, prompt chunk 6,912,
conversation cache 128 slots, Hermes **v0.23.3**.

---

## 1. Inference engine (the thing that must not die)

| # | Check | Command | Good | Notes |
|---|---|---|---|---|
| 1.1 | Service alive | `systemctl is-active strata-iq3s-server` | `active` | `Restart=always`; a crash shows as restarts, not down |
| 1.2 | Restart count | `systemctl show strata-iq3s-server -p NRestarts` | 0–2 | >3 = the `verify [batch]: layer N never rang` bug or a bad config |
| 1.3 | Loaded + vision | `curl -s localhost:8080/health` | `loaded=true images=true` | images must stay **true** — vision is never to be disabled |
| 1.4 | Batch shape | `…/metrics` → `live.parallel`, `engine.batch_groups` | 3 / 3 | the 2026-11-21 sweep: 4 slots measured *worse* per stream (B1 15.3 tok/s on the 4th vs B0's 24.1) |
| 1.5 | Expert cache | `…/metrics` → `engine.expert_cache_mib` / `expert_slots` | ~25,842 MiB / ~11,096 slots | **dropped slots = the VRAM reserve or the split changed**; it was 19,370 MiB / 6,450 slots before `--kv-resident 32768` |
| 1.6 | Prompt chunk | `grep 'prompt chunk auto' ~/local-inference/strata-src/strata-iq4xs.log \| tail -1` | **6912** tokens | below ~6,900 = a stage lost expert slots; the prefill rate falls with it. (The log file was `strata-iq3_xxs.log` until the XXS build was deleted — the name is `strata-iq4xs.log` now) |
| 1.7 | Conversation cache | `…/metrics` → `conversation_cache.parked / evictions` | parked>0, **evictions=0** | evictions rising = conversations dropped → the next turn is a cold 160–210 s prefill. The ceiling is `--conversation-cache-slots` (**128**), not the byte budget — 16 slots cost 107 evictions |
| 1.8 | "skip parking" | `grep -c 'skip parking' ~/local-inference/strata-src/strata-iq4xs.log` | 0 | means the engine **cannot save** a conversation; the floor is too high for the RAM left |
| 1.9 | Decode health | `grep 'decode timing' … \| tail -1` | **CPU experts < 6 ms/window** | with `--kv-resident` the K/V streams from RAM, so the DRAM-fed CPU path fell from ~29.9 ms to ~3 ms. A jump back to 20 ms+ means the KV ring is no longer resident |
| 1.10 | VRAM headroom | `nvidia-smi --query-gpu=index,memory.free --format=csv` | GPU0 ≥ 400 MiB | **CUDA0 runs hot**: it carries the head + the MTP drafters, so it has the least free VRAM (440 MiB today). Below ~100 MiB the engine risks `cuBLAS status 14` (it happened at 19 MiB with `--vram-reserve-mib 512`; **1024 is the floor**) |
| 1.11 | Warm-up effect | 3 back-to-back soaks | rising | the engine is 2× faster on the 3rd soak than the 1st; always compare like-for-like |

## 2. GPUs

| # | Check | Command | Good | Notes |
|---|---|---|---|---|
| 2.1 | VRAM per card | `nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv` | 11.0–11.5 / 12288 MiB | each card is deliberately nearly full (that is the expert cache) |
| 2.2 | Temperature | `nvidia-smi --query-gpu=temperature.gpu` | < 85 C | 58–75 C typical; **80 C is normal under a 200k-token prefill** and the health API no longer alerts on it alone — it alerts at 85 C or when the card is actually throttling |
| 2.3 | Power vs limit | `nvidia-smi --query-gpu=power.draw,power.limit` | 30–140 W of 170–212 W | **power is not the limiter** — cards sit well under limit while waiting on DRAM |
| 2.4 | SM utilisation | `nvidia-smi pmon -c 5` | the busiest card is a *split stage* | the x1 card (GPU3) is the *least* busy → link width is not the bottleneck |
| 2.5 | PCIe AER | `journalctl -k -b \| grep -c 'AER:.*Correctable'` **and** `cat /sys/bus/pci/devices/0000:00:1a.0/aer_dev_correctable` | flat | **two different instruments — never trend them together.** The journal count and the sysfs `TOTAL_ERR_COR` sum are not comparable. Root port `00:1a.0` carries **GPU1** (PCI 03:00.0, Lite-Hash-Rate SKU `10de:2504`) and is the marginal link; `00:1d.7` carries **GPU3** (08:00.0, the x1 M.2_2 riser) and is clean (0) after the 11-21 reseat |
| 2.6 | Xid | `journalctl -k -b \| grep Xid` | none | any Xid = a real GPU fault, stop and investigate. `Xid 79` = the card fell off the bus → the engine then refuses to start (the layer split must be re-cut for the surviving cards, or set to `auto`) |
| 2.7 | Topology | `lspci -D -t` | 4 GPUs | GPU0 `01:00.0`/x16 · GPU1 `03:00.0`/x4 · GPU2 `06:00.0`/x4 · GPU3 `08:00.0`/**x1** |

## 3. Memory / swap

| # | Check | Command | Good | Notes |
|---|---|---|---|---|
| 3.1 | MemAvailable | `grep MemAvailable /proc/meminfo` | > 4 GiB | low is *normal* on this box, but the kanban dispatcher defers below its threshold |
| 3.2 | PSI stall | `grep '^some' /proc/pressure/memory` | avg10 < 5 | the real pressure signal; free RAM is not. The engine's own reclaim pushes it to 15–19 transiently — that is the design |
| 3.3 | Swap churn | `grep pswpin /proc/vmstat` twice | flat while idle | climbing while idle = something is thrashing |
| 3.4 | Engine cgroup | `cat /sys/fs/cgroup/system.slice/strata-iq3s-server.service/memory.{current,high,max}` | current < high < max | **high 80 GiB = the reclaim trigger, max 136 GiB = the OOM line.** The engine's floor is ≈70 GiB (56 GiB pinned anon + 13.7 GiB mapped GGUF) — never set `high` below that (at 60 GiB it stalls in permanent reclaim) |
| 3.5 | swappiness | `sysctl vm.swappiness` | 60 | the owner set 60 deliberately; the parking file's 100 is overridden |
| 3.6 | Hugepages | `sysctl vm.nr_hugepages` | 0 | the 36,800-page pool was abandoned: it starved parking and measured slower (241 vs 330 tok/s) |
| 3.7 | THP | `cat /sys/kernel/mm/transparent_hugepage/enabled` | **always** | measured: cold prefill −10 %, park time −45 %. **Two units write this file**: `thp-always.service` (must win) and `crash-mitigate.service` (THP off + zswap off, a leftover mitigation from the 11-11 mm-corruption hunt — the 11-14 and 11-17 crashes both happened *with* THP off, so it never prevented anything). `thp-always.service` now carries `After=crash-mitigate.service` so the race cannot recur |
| 3.8 | Swap size | `swapon --show` | **4 files, 278 GiB** | `/swap.img` 8 G + `/srv/inference/swap/{swapfile,swapfile3,swapfile4}` 56+48+160 G. `swapfile4` is **not** created automatically — it is in `/etc/fstab` now, and it went missing once because it was only `swapon`ned by hand |

## 4. Hermes

| # | Check | Command | Good | Notes |
|---|---|---|---|---|
| 4.1 | Core services | `systemctl is-active hermes-{gateway,dashboard,hindsight,health-api,mcp-bridge}` | all active | the gateway and dashboard run the **3.14 store python** via `.hermes/bin/hermes` (the repo venv is 3.11 and cannot import `pydantic_core.ArgsKwargs`) |
| 4.2 | Kanban deferrals | `grep -c 'memory pressure is critical' ~/.hermes/logs/agent.log` | 0 | both units carry `HERMES_KANBAN_IGNORE_MEMORY_PRESSURE=1` |
| 4.3 | Plugin load | `grep 'Failed to load plugin' ~/.hermes/logs/errors.log` | none | `clara-vitals` was failing on every start (missing `__init__.py`) — fixed 03:40 |
| 4.4 | Plugin versions | `hermes plugins list --json` | conduit_push 0.179.2 | update with `hermes plugins update <name>`; `check-updates` is read-only |
| 4.5 | Boxcat = system-maintainer | `grep -r 'Boxcat Housekeeper' ~/.hermes/profiles/*/runtime/` | present | the housekeeper bot is the **system-maintainer** profile; it has the `hindsight` plugin enabled (bank `hermes`) and independently re-verifies claims — it caught the summariser's false successes |
| 4.6 | Physical console | `systemctl is-enabled freecad-gui` | disabled | the console must stay a text terminal; FreeCAD runs in the noVNC display instead |
| 4.7 | Dispatcher | `hermes kanban diagnostics` (any profile home) | `kanban.dispatch_profiles: any` | the dispatcher runs in the **gateway** (`kanban.dispatch_in_gateway: true`), ticks every 60 s, and holds `~/.hermes/kanban/.dispatcher.lock`. `dispatch_profiles` absent = any profile may claim |
| 4.8 | Dashboard | `curl -su clara:clara localhost:9119/api/status` | `gateway_running: true`, 18 profiles | the dashboard is also proxied on `:2000`; `:9119` is basic-auth, `:8788` (WebUI) is loopback-only by design |

## 5. Hindsight

| # | Check | Command | Good | Notes |
|---|---|---|---|---|
| 5.1 | API + DB | `curl -s localhost:8888/health` | `status=healthy database=connected` | pg0 needs the ICU/locale wrapper — see AGENTS.md; a pg0 upgrade breaks it |
| 5.2 | Bank stats | `curl -s localhost:8888/v1/default/banks/hermes/stats` | nodes>2500, failed_ops=0 | `failed_ops=8` are historical (the stale-model-name window on 11-20) |
| 5.3 | Backlog | same | `pending_consolidation` small | work it with `POST /v1/default/banks/hermes/consolidate` |
| 5.4 | Engine path | the LLM must be reachable | gate on `:8082` → engine | Hindsight calls go through the gate; recall is a DB query and never queues |
| 5.5 | LLM model | `grep HINDSIGHT_API_LLM_MODEL /etc/hermes-hindsight.env` | `qwen3.8-flash-next-iq4_xs` | the engine does **not** validate the model name — a deleted build name fails only at run time, so check this after any model change |

## 6. Watchers and disk

| # | Check | Command | Good | Notes |
|---|---|---|---|---|
| 6.1 | Throughput watcher | `systemctl is-active strata-throughput-watcher` + file mtime | active, < 5 min old | writes `logs/throughput-20261118.jsonl` |
| 6.2 | Stability watcher | `systemctl is-active strata-stability-watch` + file mtime | active, < 5 min old | AER/MCE/OOM/PSI/temps, and flags unclean prior boots |
| 6.3 | Disk | `df -h /srv/inference /home/clara` | > 15 % free | the old disks (`/mnt/hdd`, `/mnt/win`) are **read-only** mounts and not in fstab |
| 6.4 | Watchdog armed | `systemctl show -p RuntimeWatchdogUSec` | 30s | a wedged box reboots instead of hanging; reversal is two lines in `/etc/systemd/system.conf` |
| 6.5 | Summariser batch | `pgrep -f batch_run2.py` + `tail /tmp/opencode/batch_stdout9.txt` | running, and every line ends in a real commit | the tool verifies the **DB** before reporting success (`compacted_rows` + rotated children); a `false_success` line means the summary ran but did not land |

---

## Nightly quick pass (60 seconds)

```sh
bash /home/clara/logs/health_check.sh
```

Then, if it printed WARNs worth acting on:

```sh
# engine
K=$(cat ~/.config/opencode/strata.key); curl -s -H "Authorization: Bearer $K" localhost:8080/metrics | head -c 400
grep 'prompt chunk auto' ~/local-inference/strata-src/strata-iq4xs.log | tail -1
# AER trend (is the riser getting worse?)
journalctl -k -b | grep -c 'AER:.*Correctable'
cat /sys/bus/pci/devices/0000:00:1a.0/aer_dev_correctable | grep TOTAL_ERR_COR
# memory
grep -E 'MemAvailable|SwapFree' /proc/meminfo; grep '^some' /proc/pressure/memory
swapon --show
cat /sys/kernel/mm/transparent_hugepage/enabled
# hermes
systemctl is-active hermes-gateway hermes-dashboard hermes-hindsight
grep -c 'memory pressure is critical' ~/.hermes/logs/agent.log
# hindsight
curl -s localhost:8888/v1/default/banks/hermes/stats
```
