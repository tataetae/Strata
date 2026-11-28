#!/bin/bash
# The two things that need the owner's sudo:
#
#   1. the LAN rules for the engine's state view (9000 and 9120)
#   2. a gateway restart - the wait notice (the queue, the position, the estimate) is new
#      code in the agent modules, and the agent modules are imported when the gateway starts.
#
# Run it with:  bash /home/clara/workspace/open-strata-state-port.sh
#
# It is safe to run again: the rules are idempotent and the gateway restart is a restart.

allow_port() {
  local port="$1" note="$2"
  # ufw's parser is fussy about the comment text (an apostrophe in it is "Invalid syntax"),
  # so the note is a fallback, not a given.
  if sudo ufw --dry-run allow from 192.168.1.0/24 to any port "$port" proto tcp comment "$note" >/dev/null 2>&1; then
    sudo ufw allow from 192.168.1.0/24 to any port "$port" proto tcp comment "$note" && \
      echo "  the port $port: opened on the LAN (with the note)" && return 0
  fi
  if sudo ufw --dry-run allow from 192.168.1.0/24 to any port "$port" proto tcp >/dev/null 2>&1; then
    sudo ufw allow from 192.168.1.0/24 to any port "$port" proto tcp && \
      echo "  the port $port: opened on the LAN (ufw refused the note; the rule is the same)" && return 0
  fi
  echo "  the port $port: ufw refused the rule" >&2
  return 1
}

echo "== opening the engine's state port on the LAN (192.168.1.0/24)"
allow_port 9000 "the engine state" || echo "  (continuing: the state view stays on 9120)"
allow_port 9120 "the engine state on the LAN address" || echo "  (continuing)"

echo
echo "== restarting the gateway (the wait notice: the queue, the position, the estimate)"
sudo systemctl restart hermes-gateway
sleep 8
echo "  the gateway: $(systemctl is-active hermes-gateway)"

echo
echo "== the rules:"
sudo ufw status | grep -E "9000|9120" || echo "  (no 9000/9120 rule yet - the ufw version may refuse them; the state view still works on the LAN through the other port)"

cat <<'EOF'

the state view:  http://192.168.1.36:9000/   (the engine's bearer key is asked for)
                 http://192.168.1.36:9120/   (the same view)

one more, when the box is QUIET (it breaks the requests in flight - the proxy sits in the
request path, so a restart drops the SSE streams of everything in flight):
  sudo systemctl restart strata-clamp-proxy.service
EOF
