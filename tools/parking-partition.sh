#!/bin/bash
# The parking partition, for the owner to run under sudo.
#
# WHAT THIS IS FOR
#
# The engine's conversation cache parks every conversation it holds: a 308k-token chat is a
# 4.9 GiB snapshot (the K/V is 251 MB of it; the rest is the checkpoint chain and the per-stage
# images).  Those snapshots are anonymous memory, so over the engine's cgroup high the kernel
# writes them to the NVMe swap.  A restore then has to page them back in, and that page-in is
# the slow part of the bot chat's turn: measured 240-440 MB/s against the device's 1.1 GB/s
# sequential, so a 213k-token conversation took 26-31 s.
#
# Three things are done here, in the order of what they buy:
#
#   1. A DEDICATED SWAP FILE for the parking, at the highest priority.  The kernel allocates
#      swap first-fit across the files by priority, so the engine's snapshot pages land in this
#      file - and they land CONTIGUOUSLY in it, so the swap-in read-ahead (64 KiB) reads useful
#      data instead of the other processes' interleaved pages.  This is the "swap-optimize
#      sequential partition" the fix is built around.
#   2. The engine's cgroup MemoryHigh 88 -> 96 GiB, so the HOT snapshots (the ones the bots are
#      using) stay in RAM and never page out at all.  The AGENTS.md measured this before: at
#      96 GiB the restores were RAM-speed (0.4-3 s) - the cost was the host's free memory,
#      which is why it was dialled back to 88.
#   3. A modest TRANSPARENT-hugepage pool is NOT reserved here: the engine asks for THP with
#      madvise(MADV_HUGEPAGE) (the 27 November change), and THP is 'always' on this box, so
#      no pool is needed.  The explicit hugetlbfs pool stays OFF on purpose: the AGENTS.md
#      measured it as harmful (it needs 28 796 pages = 56 GiB all-or-nothing, it starved the
#      parking, and the prefill measured 241 vs 330 tok/s).
#
# IT IS REVERSIBLE: every step prints what it changed, and the swap file can be removed with
#   swapoff /srv/inference/swap/swapfile5 && rm /srv/inference/swap/swapfile5
#
set -u

SWAPFILE=/srv/inference/swap/swapfile5
SWAPSIZE=64G
HIGH=96G
LOW=72G
MAX=136G

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
run() { printf '   $ %s\n' "$*"; "$@"; }

[ "$(id -u)" = 0 ] || { echo "run me with sudo"; exit 1; }

say "the state before"
free -g | sed 's/^/   /'
swapon --show | sed 's/^/   /'
systemctl show strata-iq3s-server -p MemoryHigh -p MemoryMax 2>/dev/null | sed 's/^/   /'

say "1. the dedicated swap file for the parking"
if swapon --show=NAME --noheadings | grep -qx "$SWAPFILE"; then
    echo "   $SWAPFILE is already on"
else
    if [ ! -f "$SWAPFILE" ]; then
        echo "   creating $SWAPFILE ($SWAPSIZE)"
        fallocate -l "$SWAPSIZE" "$SWAPFILE" || dd if=/dev/zero of="$SWAPFILE" bs=1M count=65536 status=none
        chmod 600 "$SWAPFILE"
        mkswap "$SWAPFILE" >/dev/null
    fi
    # priority 100: the kernel fills this file before the others, so the engine's snapshot
    # pages are contiguous in it
    swapon -p 100 "$SWAPFILE" && echo "   $SWAPFILE is on at priority 100"
    if ! grep -qF "$SWAPFILE" /etc/fstab; then
        printf '%s none swap sw,prio=100 0 0\n' "$SWAPFILE" >> /etc/fstab
        echo "   added to /etc/fstab (prio=100)"
    fi
fi

say "2. the engine's memory high (the hot snapshots stay in RAM)"
run systemctl set-property strata-iq3s-server.service "MemoryHigh=$HIGH" "MemoryLow=$LOW" "MemoryMax=$MAX"
systemctl show strata-iq3s-server -p MemoryHigh -p MemoryMax | sed 's/^/   /'

say "3. the transparent huge pages"
printf '   THP: %s\n' "$(cat /sys/kernel/mm/transparent_hugepage/enabled)"
printf '   defrag: %s\n' "$(cat /sys/kernel/mm/transparent_hugepage/defrag)"

say "the state after"
free -g | sed 's/^/   /'
swapon --show | sed 's/^/   /'
systemctl show strata-iq3s-server -p MemoryHigh -p MemoryMax | sed 's/^/   /'

cat <<'EOF'

WHAT TO WATCH

  The engine logs its restores:
      grep "conversation cache: restored" /home/clara/local-inference/strata-src/strata-iq4xs.log | tail
  A RAM-speed restore is under ~3 s; the swap path was 5-31 s (and up to 96 s).

  And the prefetch, which pulls the pages back while the request waits for a slot:
      grep "conversation cache: prefetched" /home/clara/local-inference/strata-src/strata-iq4xs.log | tail

  The host's free memory is the cost of step 2: watch `free -g`.  If the host drops under
  about 4 GiB free, set the high back:
      systemctl set-property strata-iq3s-server.service MemoryHigh=88G
EOF
