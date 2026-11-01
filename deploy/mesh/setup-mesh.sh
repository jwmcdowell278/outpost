#!/bin/bash
# Runs ON the target machine as root. Installs the Wi-Fi/Ethernet batman-adv
# mesh (outpost-mesh + outpost-mesh.service). Started by install-node.sh:
#   setup-mesh.sh <node-dir>
# node-dir holds mesh/ and lib/pkg.sh, and optionally mesh.env (shared
# settings; copy /etc/outpost-mesh/mesh.env from an existing node).
# Env: MESH_WIFI / MESH_ETH - interfaces to use; MESH_SELECT=1 - pick them
# from a menu instead; neither - keep the current ones. FORCE_MESH=1 allows
# an interface this machine is reached through. SKIP_PACKAGES=1.
set -euo pipefail

N=${1:?usage: setup-mesh.sh <node-dir>}
[ "$(id -u)" = 0 ] || { echo "!! run as root" >&2; exit 1; }
. "$N/lib/pkg.sh"
ETC=/etc/outpost-mesh

if [ "${SKIP_PACKAGES:-0}" != 1 ]; then
    echo "== mesh packages"
    pkg_detect
    pkg_need mesh
    pkg_want menu
fi
for t in batctl iw wpa_supplicant ip; do
    command -v "$t" >/dev/null || { echo "!! $t missing" >&2; exit 1; }
done
modprobe batman-adv 2>/dev/null || {
    echo "!! this kernel has no batman-adv module (on Fedora: install kernel-modules-extra and reboot)" >&2; exit 1; }

echo "== installing outpost-mesh"
install -m 0755 "$N/mesh/outpost-mesh" /usr/local/sbin/outpost-mesh
install -m 0644 "$N/mesh/outpost-mesh.service" /etc/systemd/system/outpost-mesh.service
mkdir -p "$ETC"
if [ -f "$N/mesh.env" ]; then
    install -m 0600 "$N/mesh.env" "$ETC/mesh.env"
    echo "   shared mesh settings installed"
elif [ ! -f "$ETC/mesh.env" ]; then
    install -m 0600 "$N/mesh/mesh.env" "$ETC/mesh.env"
    pw=$(head -c 18 /dev/urandom | base64 | tr -d '/+=')
    sed -i "s/^MESH_PASSWORD=.*/MESH_PASSWORD=$pw/" "$ETC/mesh.env"
    echo "   new mesh password - copy $ETC/mesh.env to the other nodes"
fi
grep -q '^MESH_PASSWORD=.' "$ETC/mesh.env" || { echo "!! MESH_PASSWORD is empty in $ETC/mesh.env" >&2; exit 1; }
# Keep the HaLow and Wi-Fi meshes one network when both are on this machine.
if [ -f /etc/halow-mesh/halow.env ]; then
    for k in BATMAN_ALGO BAT_PREFIX; do
        v=$(sed -n "s/^$k=//p" /etc/halow-mesh/halow.env | tr -d "'\"")
        [ -z "$v" ] || sed -i "s/^$k=.*/$k=$v/" "$ETC/mesh.env"
    done
fi
[ -f "$ETC/node.env" ] || printf 'MESH_WIFI=""\nMESH_ETH=""\n' > "$ETC/node.env"
echo batman-adv > /etc/modules-load.d/batman-adv.conf
systemctl daemon-reload

# shellcheck disable=SC2086
if [ -n "${MESH_WIFI:-}${MESH_ETH:-}" ]; then
    force=; [ "${FORCE_MESH:-0}" = 1 ] && force=--force
    /usr/local/sbin/outpost-mesh use $force ${MESH_WIFI:-} ${MESH_ETH:-}
elif [ "${MESH_SELECT:-0}" = 1 ]; then
    /usr/local/sbin/outpost-mesh select
elif grep -q '="[^"]' "$ETC/node.env"; then
    systemctl restart outpost-mesh.service || echo "!! outpost-mesh didn't start - see: journalctl -u outpost-mesh" >&2
    echo "   keeping the current mesh interfaces"
else
    echo "   no mesh interfaces chosen yet - run: sudo outpost-mesh select"
fi
echo "== mesh installed"
