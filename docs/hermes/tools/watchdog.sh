#!/bin/bash
# The summariser batch's watchdog.
#
# WHY THIS IS STRICT: the first version stopped the batch after a SINGLE 10 s
# /health timeout, on a perfectly healthy engine that answered in 0.00 s the
# moment after (the engine was mid-prefill on a 300k-token prompt). A busy
# engine legitimately times out on /health - that is the engine working, not
# failing. v2 needed 3 consecutive failures.
#
# v3 (Boxcat's rule, accepted): a /health failure only counts when the engine
# PROCESS IS ALSO GONE. So the batch is stopped only when the engine unit is
# inactive - i.e. the engine really died - or when the host itself is in
# trouble (PSI memory avg10 > 40, or MemAvailable < 2 GiB).
#
# It never stops the batch just because the batch finished, and it never stops
# it for a single timeout, however many.
#
# Log: /tmp/opencode/watchdog.log

LOG=/tmp/opencode/watchdog.log
BATCH_PROG='batch_run2[.]py'
ENGINE_UNIT=strata-iq3s-server
HEALTH_URL=http://127.0.0.1:8080/health
PROBE_TIMEOUT=8
NEEDED=3
PSI_LIMIT=40
MEMAVAIL_LIMIT_MIB=2048

log() { echo "$(date -u '+%Y-%m-%d %H:%M:%S') $*" >>"$LOG"; }

batch_alive() { pgrep -f "$BATCH_PROG" >/dev/null 2>&1; }

consec=0
log "watchdog v3 started (engine-process rule, PSI>$PSI_LIMIT, MemAvailable<${MEMAVAIL_LIMIT_MIB}MiB)"

while true; do
    if ! batch_alive; then
        log "the batch is gone - the watchdog exits"
        exit 0
    fi

    # 1. the engine's own liveness: the systemd unit, not a curl
    engine_state=$(systemctl is-active "$ENGINE_UNIT" 2>/dev/null)
    engine_pid=$(systemctl show "$ENGINE_UNIT" -p MainPID --value 2>/dev/null)

    # 2. the /health probe
    if timeout "$PROBE_TIMEOUT" curl -sS -o /dev/null -f "$HEALTH_URL" 2>/dev/null; then
        consec=0
    else
        consec=$((consec + 1))
    fi

    # 3. the host's pressure
    psi=$(awk '/^some/ {print $2}' /proc/pressure/memory 2>/dev/null | cut -d= -f2 | cut -d' ' -f1)
    memavail=$(awk '/^MemAvailable:/ {print int($2/1024)}' /proc/meminfo 2>/dev/null)

    if [ "$consec" -ge "$NEEDED" ] && [ "$engine_state" != "active" ]; then
        log "STOP the batch: /health failed ${consec}x AND the engine unit is '${engine_state}' (pid '${engine_pid}')"
        pkill -f "$BATCH_PROG" 2>/dev/null
        log "the batch was stopped"
        exit 0
    fi

    psi_int=${psi:-0}
    mem_int=${memavail:-0}
    if [ "$psi_int" -gt "$PSI_LIMIT" ]; then
        log "STOP the batch: PSI memory avg10=$psi_int (>$PSI_LIMIT)"
        pkill -f "$BATCH_PROG" 2>/dev/null
        exit 0
    fi
    if [ "$mem_int" -lt "$MEMAVAIL_LIMIT_MIB" ]; then
        log "STOP the batch: MemAvailable=${mem_int}MiB (<${MEMAVAIL_LIMIT_MIB}MiB)"
        pkill -f "$BATCH_PROG" 2>/dev/null
        exit 0
    fi

    log "ok engine=${engine_state} pid=${engine_pid} consec_fail=${consec}/${NEEDED} psi=${psi_int} mem=${mem_int}MiB"
    sleep 60
done
