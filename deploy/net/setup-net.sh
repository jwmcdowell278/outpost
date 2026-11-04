#!/bin/bash
# Runs ON the target machine as root: installs outpost-net (forwarding and
# NAT gateways). Started by install-node.sh:  setup-net.sh <node-dir>
set -euo pipefail
N=${1:?usage: setup-net.sh <node-dir>}
[ "$(id -u)" = 0 ] || { echo "!! run as root" >&2; exit 1; }
. "$N/lib/pkg.sh"
if [ "${SKIP_PACKAGES:-0}" != 1 ]; then pkg_detect && pkg_want firewall menu dns nettools; fi
PATH=$PATH:/usr/sbin:/sbin
command -v nft >/dev/null || { echo "!! nftables (nft) missing - outpost-net not installed" >&2; exit 0; }
install -m 0755 "$N/net/outpost-net" /usr/local/sbin/outpost-net
for u in outpost-net.service outpost-net-route.service outpost-net-route.timer outpost-net-dns.service; do
    install -m 0644 "$N/net/$u" "/etc/systemd/system/$u"
done
systemctl daemon-reload || true
if systemctl enable outpost-net.service outpost-net-route.timer >/dev/null 2>&1 \
        && systemctl restart outpost-net.service && systemctl start outpost-net-route.timer; then
    echo "== outpost-net installed (sudo outpost-net)"
else
    echo "!! outpost-net installed, but its service didn't start - see: systemctl status outpost-net" >&2
fi
