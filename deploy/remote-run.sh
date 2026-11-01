#!/bin/bash
# Runs ON the target machine as the login user, from the bundle that
# install-node.sh copies over (settings in vars.sh next to this file).
set -euo pipefail
D=$(cd "$(dirname "$0")" && pwd)
. "$D/vars.sh"

if [ "$HALOW" = 1 ]; then
    sudo bash "$D/halow-kit/pi-install.sh" "$D/halow-kit"
fi
# Wi-Fi/Ethernet mesh tool. Always installed; a failure only stops the
# install if mesh cards were asked for.
if ! sudo env MESH_WIFI="$MESH_WIFI" MESH_ETH="$MESH_ETH" FORCE_MESH="$FORCE_MESH" MESH_SELECT="$MESH_SELECT" TERM="${TERM:-dumb}" \
        SSH_CONNECTION="${SSH_CONNECTION:-}" bash "$D/mesh/setup-mesh.sh" "$D"; then
    if [ "$MESH_ASKED" = 1 ]; then exit 1; fi
    echo "== outpost-mesh skipped (see above) - the rest of the install carries on"
fi
NODE_NAME="$NODE_NAME" USB_BT="$USB_BT" AUTOSTART="$AUTOSTART" bash "$D/setup-outpost.sh" "$D"

# Forwarding and NAT gateways.
sudo bash "$D/net/setup-net.sh" "$D" || echo "!! outpost-net setup failed (see above) - the rest of the install carries on"
if [ "$NET_SETUP" = 1 ]; then sudo env TERM="${TERM:-dumb}" /usr/local/sbin/outpost-net || true; fi

# Wi-Fi client/access point tool - needs NetworkManager.
if command -v nmcli >/dev/null; then
    sudo install -m 0755 "$D/wifi/outpost-wifi" /usr/local/sbin/outpost-wifi
    echo "== outpost-wifi installed (sudo outpost-wifi)"
    if [ "$WIFI_SETUP" = 1 ]; then sudo env TERM="${TERM:-dumb}" /usr/local/sbin/outpost-wifi || true; fi
else
    echo "== outpost-wifi not installed: it needs NetworkManager, which this machine doesn't use"
fi
