#!/bin/bash
# Stop the summariser batch if the ENGINE is genuinely down.
# /health TIMES OUT UNDER LOAD, so require 3 CONSECUTIVE failed probes.
LOG=/tmp/opencode/watchdog.log
consec=0
while true; do
  sleep 240
  ts=$(date -u +%H:%M:%S)
  body=$(curl -sS -m 8 http://127.0.0.1:8080/health 2>/dev/null)
  if echo "$body" | grep -q '"loaded":true'; then ok=1; consec=0; else ok=0; consec=$((consec+1)); fi
  mem=$(awk '/MemAvailable/{printf "%d",$2/1024}' /proc/meminfo)
  psi=$(awk '/^some/{for(i=1;i<=NF;i++) if($i ~ /^avg10=/){split($i,a,"=");print a[2]; exit}}' /proc/pressure/memory)
  batch=$(pgrep -cf 'batch_run2[.]py' 2>/dev/null || echo 0)
  echo "$ts health=$ok consec_fail=$consec memavail=${mem}MiB psi_avg10=${psi:-?} batchprocs=$batch" >> "$LOG"
  if [ "$batch" = "0" ]; then
    echo "$ts batch not running -> the watchdog exits" >> "$LOG"; exit 0
  fi
  if [ "$consec" -ge 3 ]; then
    echo "$ts 3 CONSECUTIVE health failures -> stopping the batch" >> "$LOG"
    pkill -f 'batch_run2[.]py' 2>/dev/null; pkill -f 'run_compress[.]sh' 2>/dev/null
    exit 0
  fi
  if awk -v p="${psi:-0}" 'BEGIN{exit !(p>40)}' 2>/dev/null; then
    echo "$ts PSI memory avg10=$psi -> stopping the batch" >> "$LOG"
    pkill -f 'batch_run2[.]py' 2>/dev/null; pkill -f 'run_compress[.]sh' 2>/dev/null
    exit 0
  fi
  if [ "${mem:-0}" -lt 2000 ]; then
    echo "$ts MemAvailable ${mem}MiB < 2 GiB -> stopping the batch" >> "$LOG"
    pkill -f 'batch_run2[.]py' 2>/dev/null; pkill -f 'run_compress[.]sh' 2>/dev/null
    exit 0
  fi
done
