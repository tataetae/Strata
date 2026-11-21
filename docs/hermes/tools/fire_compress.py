#!/usr/bin/env python
"""Fire the Hermes summariser on a session, standalone (no gateway, no dashboard).

Mirrors gateway/slash_commands_session.py:_run_manual_compression:
  build a throwaway AIAgent on the profile's own route, run the shared compress_now()
  core over the persisted transcript, and commit the rotation.
"""
import asyncio, json, os, sys, pathlib, traceback

REPO = '/home/clara/.hermes/hermes-agent'
sys.path.insert(0, REPO)

LIVE = 'qwen3.8-flash-next-iq4_xs'
STRATA_URL = 'http://127.0.0.1:8081/v1'
def _strata_key() -> str:
    """The local engine's key, from the profile's own config (never hard-coded)."""
    import yaml
    for path in ('/home/clara/.hermes/config.yaml',
                 os.path.join(os.environ.get('HERMES_HOME', ''), 'config.yaml')):
        try:
            cfg = yaml.safe_load(open(path)) or {}
            key = ((cfg.get('providers') or {}).get('strata') or {}).get('api_key')
            if key:
                return key
        except Exception:
            continue
    raise RuntimeError('the strata provider key is not in the config')

def log(*a):
    print(*a, flush=True)

async def compress_session(profile_home: str, session_id: str, focus: str = ""):
    os.environ['HERMES_HOME'] = profile_home
    sys.path.insert(0, '/tmp/opencode')
    from hermes_cli.config import load_config
    from store import Store
    from run_agent import AIAgent
    from agent.conversation_compression_manual import (
        compress_now, parse_compress_args, render_compress_result, MIN_MESSAGES)

    cfg = load_config() or {}
    db = Store(os.path.join(profile_home, 'state.db'))
    rows = db.read(
        "select id, model, model_config, source, title, system_prompt from sessions where id=?",
        (session_id,))
    if not rows:
        log("  !! session not found: %s" % session_id); db.close(); return None
    row = rows[0]
    msgs = db.read(
        "select role, content, tool_calls, tool_call_id, tool_name, reasoning_content "
        "from messages where session_id=? and active=1 order by id", (session_id,))
    if len(msgs) < MIN_MESSAGES:
        log("  -- only %d active messages (< MIN_MESSAGES=%d), skipping" % (len(msgs), MIN_MESSAGES))
        db.close(); return None

    try: mc = json.loads(row[2]) if row[2] else {}
    except Exception: mc = {}
    provider = mc.get('provider') or 'strata'
    base_url = mc.get('base_url') or STRATA_URL
    model = row[1] or LIVE

    history = []
    for role, content, tool_calls, tool_call_id, tool_name, reasoning in msgs:
        m = {'role': role}
        if content: m['content'] = content
        if tool_calls:
            try: m['tool_calls'] = json.loads(tool_calls) if isinstance(tool_calls, str) else tool_calls
            except Exception: pass
        if tool_call_id: m['tool_call_id'] = tool_call_id
        if tool_name: m['name'] = tool_name
        if reasoning: m['reasoning_content'] = reasoning
        history.append(m)

    runtime = dict(
        provider=provider, base_url=base_url, api_key=_strata_key(), api_mode='chat_completions',
        reasoning_config={'enabled': True, 'effort': 'none'},
    )
    request = parse_compress_args(focus)
    # v0.23+: the agent wants the persistence handle; v0.22 and earlier: the SessionDB.
    agent_kwargs = {}
    if getattr(db, '_p', None) is not None:
        agent_kwargs['persistence'] = db._p
    else:
        agent_kwargs['session_db'] = db._db
    agent = AIAgent(**runtime, model=model, max_iterations=4, quiet_mode=True,
                    skip_memory=True, enabled_toolsets=[], session_id=session_id,
                    **agent_kwargs)
    agent._print_fn = lambda *a, **kw: None
    agent._end_session_on_close = False
    try:
        result = compress_now(agent, history, request, system_message="",
                               skip_without_window=True, task_id=session_id or "default")
        summ = result.summary if isinstance(result.summary, dict) else {}
        body = summ.get('summary') or summ.get('text') or summ.get('content') or json.dumps(summ, default=str)
        log("  status=%s  before=%d tok  after=%d tok  removed=%d msgs" % (
            result.status, result.before_tokens, result.after_tokens, result.removed))
        log("  summary: %s" % str(body)[:300].replace('\n', ' '))
        if result.status != "compressed":
            for line in render_compress_result(result):
                log("    %s" % line)
            return result.status
        # COMMIT CHECK: the tool must VERIFY the database actually changed before
        # reporting success. result.status only says the summariser ran; only the DB says
        # the archive landed. A "compressed" report with compacted=0 rows is a FALSE
        # SUCCESS (13 sessions were reported compressed and never were).
        new_id = db.read(
            "select id, parent_session_id, end_reason from sessions where id=? or parent_session_id=?",
            (session_id, session_id))
        live = db.read(
            "select count(*) from messages where session_id=? and active=1", (session_id,))[0][0]
        compacted_rows = db.read(
            "select count(*) from messages where session_id=? and compacted=1", (session_id,))[0][0]
        rotated = [r for r in new_id if r[0] != session_id]
        log("  after: %d messages in the live session (was %d)" % (live, len(msgs)))
        log("  COMMIT CHECK: compacted_rows=%d rotated_children=%d" % (compacted_rows, len(rotated)))
        for r in new_id:
            log("    session %s parent=%s end=%s" % (r[0][:40], r[1], r[2]))
        if compacted_rows == 0 and not rotated:
            log("  !! FALSE SUCCESS: the summariser ran but nothing was archived or rotated.")
            return "false_success"
        return "compressed"
    except Exception as e:
        log("  !! FAILED: %s" % e)
        traceback.print_exc()
        return "failed"
    finally:
        try: agent.close()
        except Exception: pass
        try: db.close()
        except Exception: pass

if __name__ == "__main__":
    home, sid = sys.argv[1], sys.argv[2]
    focus = sys.argv[3] if len(sys.argv) > 3 else ""
    r = asyncio.run(compress_session(home, sid, focus))
    log("  RESULT: %s" % r)
