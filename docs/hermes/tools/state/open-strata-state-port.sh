#!/bin/bash
# Open the engine's state view on the LAN, and refresh the services that read it.
#
# The state server (strata-state-server.service, a USER unit) is already listening:
#   0.0.0.0:9000        the port the owner asked for
#   192.168.1.36:9120   the same view on the LAN address (the health API keeps 127.0.0.1:9120)
#
# UFW denies both from the LAN today (measured from clara-serv-01: 8080 and 9119 answer,
# 9000 and 9120 are closed).  These rules open them to the LAN only - the key is still
# required for everything but /health.
#
# Run it with:  bash /home/clara/workspace/open-strata-state-port.sh
set -e
echo "== opening the engine's state port on the LAN (192.168.1.0/24)"
sudo ufw allow from 192.168.1.0/24 to any port 9000 proto tcp comment "the engine's state"
sudo ufw allow from 192.168.1.0/24 to any port 9120 proto tcp comment "the engine's state (the LAN address)"

echo
echo "== refreshing the phone push (it now shows each chat's place in the queue)"
sudo systemctl restart strata-queue-push.service
systemctl is-active strata-queue-push.service

echo
echo "== the rules:"
sudo ufw status | grep -E "9000|9120" || true

cat <<'EOF'

the state view:  http://192.168.1.36:9000/   (the engine's bearer key is asked for)
                 http://192.168.1.36:9120/   (the same view)

one more, when the box is QUIET (it breaks the requests in flight - the proxy sits in the
request path, so a restart drops the SSE streams of everything in flight):
  sudo systemctl restart strata-clamp-proxy.service
EOF
