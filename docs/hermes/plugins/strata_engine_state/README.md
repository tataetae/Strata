# The `strata_engine_state` plugin (Hermes)

A local Hermes plugin that lets an agent read the **local inference engine's live state**:
the queue depth, the slots, the prompt being read, the conversation cache's reuse, this
chat's place in the queue and an estimate of the wait.

It is a plugin, not a core tool: Hermes sends every tool's schema on every model call, and
the bar for a new core tool is high. The capability arrives at the edge instead.

## What it reads

| what | where |
|---|---|
| the engine's live state (queue, slots, cache, rates) | `GET $STRATA_ENGINE_URL/metrics` (default `http://127.0.0.1:8080`), with the bearer key from `$STRATA_ENGINE_KEY_FILE` (default `~/.config/opencode/strata.key`) |
| the per-chat queue view (which chat waits where) | `GET $STRATA_QUEUE_VIEW_URL` (default `http://127.0.0.1:8081/queue`) — the clamp proxy's own endpoint |

Both are read-only. The plugin never changes the engine, the queue or a conversation.

## The install (per profile)

The plugin lives in the Hermes home, not in the hermes-agent tree:

```sh
cp -r docs/hermes/plugins/strata_engine_state ~/.hermes/plugins/
```

`plugins.enabled` is an **allow-list, per profile**, so the plugin must also be named in
every profile that should load it:

```yaml
plugins:
  enabled:
    - strata_engine_state
```

All 21 configs on this box (the root plus 20 profiles) carry it, with
`config.yaml.bak-<stamp>-strata_engine_state` beside each.

The toolset (`strata-engine`) is **not** in Hermes's default-off list, so it is enabled on
every platform without a `toolsets:` entry.

## When it goes live

`hermes plugins` loads a plugin's handlers at once (a `reload-plugins` on the gateway's
control socket forces re-discovery); its **tools land in the next session**, so a bot needs a
fresh session before it can call `strata_engine_state`.

## The queue depth (why `waiting` is not the depth)

The engine's `/metrics` publishes `live.waiting` and `live.admitting`. Each names ONE
phase of the queue — a request in the control-line queue, a request that gave the lines back
and waits for a slot — and either can read 0 while a dozen requests are waiting. The depth
is the requests in flight that do **not** hold a slot:

```
depth = live.running - (the slots whose state is not idle)
```

Measured on this box: `running=18`, `busy_slots=2` → 16 waiting, while `live.waiting` read
16 and `live.admitting` read 11 in the same sample.

## The estimate

Two terms, both from the engine's own numbers:

- the read still to come: `(total - processed) / live.prefill_tok_s_mean`;
- the requests ahead: `(ahead / slots) * mean_service`, where `mean_service` is the
  engine's own mean over its history: `(totals.prompt_ms + totals.decode_ms) / totals.requests`.

Both are averages over the engine's history, so the result is an estimate and is shown as
one ("about 4 min").
