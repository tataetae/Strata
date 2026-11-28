#!/bin/bash
# Open the engine's state view on the LAN.
#
# The state server (strata-state-server.service, a user unit) is already listening:
#   0.0.0.0:9000        the port the owner asked for
#   192.168.1.36:9120   the same view on the LAN address (the health API keeps 127.0.0.1:9120)
#
# UFW denies both from the LAN today (measured from clara-serv-01: 8080 and 9119 answer,
# 9000 and 9120 are closed).  These two rules open them to the LAN only - the key is still
# required for everything but /health.
#
# Run it with:  bash /home/clara/workspace/open-strata-state-port.sh
set -e
echo "opening the engine's state port on the LAN (192.168.1.0/24)"
sudo ufw allow from 192.168.1.0/24 to any port 9000 proto tcp comment "the engine's state"
sudo ufw allow from 192.168.1.0/24 to any port 9120 proto tcp comment "the engine's state (the LAN address)"
echo
echo "the rules:"
sudo ufw status | grep -E "9000|9120" || true
echo
echo "the state view:  http://192.168.1.36:9000/   (the engine's bearer key is asked for)"
