#!/bin/bash
# Sets up a Linux machine as an Outpost mesh node, over SSH from this computer.
# Works on any systemd Linux with apt, dnf, pacman or zypper (Raspberry Pi OS,
# Debian, Ubuntu, Fedora, Arch, openSUSE...):
#   - Outpost, Bluetooth headset support (Inrico PTT) and a boot-time service
#   - on a 64-bit Raspberry Pi OS Pi: the HaLow HAT drivers + batman-adv mesh
#   - optionally: a batman-adv mesh over ordinary Wi-Fi or Ethernet
#
#   ./install-node.sh <user@host> [options]     install on another machine (SSH)
#   ./install-node.sh local [options]           install on this machine
#   (on a fresh machine without the repo, bootstrap.sh downloads it and runs
#    "install-node.sh local" - see deploy/README.md)
#
#   --config FILE      the shared Outpost config (channel keys, mesh key,
#                      button bindings...), installed with sender_alias set to
#                      the node's name. Needed on the first install; leave it
#                      out later to update the code and keep the config.
#   --name NAME        node name shown to others (default: its hostname)
#   --no-autostart     install Outpost but don't start it at boot (start it
#                      with: systemctl --user start outpost)
#
#   HaLow (Raspberry Pi with a Heltec HT-HC01P; installed by default on a Pi):
#   --env FILE         HaLow mesh settings (/etc/halow-mesh/halow.env from an
#                      existing node). Leave out to keep the Pi's settings.
#   --no-halow         don't install the HaLow drivers on a Pi
#
#   Wi-Fi / Ethernet mesh (any machine, also alongside HaLow to bridge them):
#   --mesh-wifi IFACE  mesh over this Wi-Fi interface (802.11s); repeatable.
#                      Use a spare adapter - the interface is taken over.
#   --mesh-eth IFACE   put this Ethernet interface in the mesh; repeatable
#   --mesh-select      choose the mesh interfaces from a menu during the install
#                      (later, on the machine: sudo outpost-mesh select)
#   --mesh-env FILE    shared settings (/etc/outpost-mesh/mesh.env from an
#                      existing node). Without it the first node makes a new
#                      password - copy that file to the others.
#   --force-mesh       allow an interface the machine is reached through
#
#   Wi-Fi (client or access point; any machine with NetworkManager):
#   --wifi-setup       open the Wi-Fi menu at the end of the install
#                      (later, on the machine: sudo outpost-wifi)
#
#   Forwarding and gateways (NAT) - installed on every machine:
#   --net-setup        open the forwarding/gateway menu at the end
#                      (later, on the machine: sudo outpost-net)
#
#   --usb-bluetooth    Raspberry Pi: turn off the built-in Bluetooth so a USB
#                      adapter is used (recommended on a Pi 3 with a headset)
#   --reboot           reboot when done (needed after the first install)
#
# The machine needs internet access during the install and sudo (without a
# password, or run with an SSH terminal: -t is used so sudo can ask).
set -euo pipefail

usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; exit 2; }
HOST=${1:-}; [ -n "$HOST" ] && [ "${HOST#-}" = "$HOST" ] || usage
shift
LOCAL=0; [ "$HOST" = local ] && LOCAL=1
if [ "$LOCAL" = 1 ] && [ "$(id -u)" = 0 ]; then
    echo "run this as your normal user (the one Outpost runs as) - it uses sudo itself" >&2; exit 1
fi
on_target() {  # run a shell snippet on the machine being set up
    if [ "$LOCAL" = 1 ]; then bash -c "$1"; else ssh "$HOST" "$1"; fi
}
CONFIG=; ENV_FILE=; MESH_ENV=; NAME=; USB_BT=0; HALOW=auto; REBOOT=0
MESH_WIFI=; MESH_ETH=; FORCE_MESH=0; MESH_SELECT=0; WIFI_SETUP=0; NET_SETUP=0; AUTOSTART=1
while [ $# -gt 0 ]; do
    case "$1" in
        --config) CONFIG=${2:?}; shift ;;
        --name) NAME=${2:?}; shift ;;
        --no-autostart) AUTOSTART=0 ;;
        --env) ENV_FILE=${2:?}; shift ;;
        --no-halow) HALOW=0 ;;
        --mesh-wifi) MESH_WIFI="$MESH_WIFI ${2:?}"; shift ;;
        --mesh-eth) MESH_ETH="$MESH_ETH ${2:?}"; shift ;;
        --mesh-env) MESH_ENV=${2:?}; shift ;;
        --mesh-select) MESH_SELECT=1 ;;
        --wifi-setup) WIFI_SETUP=1 ;;
        --net-setup) NET_SETUP=1 ;;
        --force-mesh) FORCE_MESH=1 ;;
        --usb-bluetooth) USB_BT=1 ;;
        --reboot) REBOOT=1 ;;
        -h|--help) usage ;;
        *) echo "unknown option $1" >&2; usage ;;
    esac
    shift
done
MESH_WIFI=${MESH_WIFI# }; MESH_ETH=${MESH_ETH# }
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
for f in "$CONFIG" "$ENV_FILE" "$MESH_ENV"; do
    [ -z "$f" ] || [ -f "$f" ] || { echo "$f not found" >&2; exit 1; }
done
if [ -n "$NAME" ] && ! [[ "$NAME" =~ ^[A-Za-z0-9._\ -]{1,32}$ ]]; then
    echo "--name: use up to 32 letters, digits, spaces, dots, dashes or underscores" >&2; exit 1
fi
for i in $MESH_WIFI $MESH_ETH; do
    [[ "$i" =~ ^[A-Za-z0-9._-]{1,15}$ ]] || { echo "bad interface name: $i" >&2; exit 1; }
done

[ "$LOCAL" = 1 ] && HOST="this machine"
echo "== checking $HOST"
info=$(on_target 'uname -m; . /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-unknown Linux}";
    tr -d "\0" 2>/dev/null < /proc/device-tree/model; echo;
    command -v systemctl >/dev/null && echo systemd || echo no-systemd;
    command -v apt-get >/dev/null && echo apt || echo no-apt;
    [ -f /boot/firmware/config.txt ] && echo pi-boot || echo no-pi-boot;
    [ -d /etc/outpost-mesh ] && echo mesh-installed || echo no-mesh')
{ read -r ARCH; read -r OS; read -r MODEL; read -r INIT; read -r APT; read -r PIBOOT; read -r MESHED; } <<<"$info"
echo "   $OS, $ARCH${MODEL:+, $MODEL}"
[ "$INIT" = systemd ] || { echo "!! $HOST doesn't use systemd - not supported" >&2; exit 1; }

IS_PI=0; case "$MODEL" in *"Raspberry Pi"*) IS_PI=1 ;; esac
if [ "$HALOW" = auto ]; then
    HALOW=0
    if [ "$IS_PI" = 1 ]; then
        case "$MODEL" in *"Raspberry Pi 5"*|*"Compute Module 5"*)
            echo "   HaLow: skipped - the HaLow kit is for a Pi 3/4, not a Pi 5" ;;
        *)
            if [ "$ARCH" != aarch64 ] || [ "$APT" != apt ] || [ "$PIBOOT" != pi-boot ]; then
                echo "   HaLow: skipped - needs 64-bit Raspberry Pi OS"
            else
                HALOW=1
            fi ;;
        esac
    fi
fi
if [ "$HALOW" = 1 ] && [ ! -f "$HERE/halow/out/KERNEL_RELEASE" ]; then
    if [ "$LOCAL" = 1 ]; then
        echo "   HaLow: skipped - the HaLow kit isn't available (see deploy/halow/README.md)"
        HALOW=0
    else
        echo "!! the HaLow kit isn't built yet - see deploy/halow/README.md (Build), or use --no-halow" >&2
        exit 1
    fi
fi
# outpost-mesh is always installed (so every node has the full menu). It
# only has to succeed when mesh cards were asked for, or it's already in use.
MESH_ASKED=0
if [ -n "$MESH_WIFI$MESH_ETH$MESH_ENV" ] || [ "$MESH_SELECT" = 1 ] || [ "$MESHED" = mesh-installed ]; then MESH_ASKED=1; fi
if [ "$MESH_SELECT" = 1 ] && [ -n "$MESH_WIFI$MESH_ETH" ]; then
    echo "use either --mesh-select or --mesh-wifi/--mesh-eth" >&2; exit 2
fi
parts=Outpost
[ "$HALOW" = 0 ] || parts="$parts, HaLow drivers + mesh"
parts="$parts, network tools (outpost-net, outpost-mesh, outpost-wifi)"
[ -z "$MESH_WIFI$MESH_ETH" ] && [ "$MESH_SELECT" = 0 ] || parts="$parts, Wi-Fi/Ethernet mesh"
echo "   installing: $parts"

# bsdtar (macOS) would add Apple metadata files; GNU tar has no such flags.
TARFLAGS=(); if tar --version 2>/dev/null | grep -q bsdtar; then TARFLAGS=(--no-xattrs --no-mac-metadata); fi
export COPYFILE_DISABLE=1

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
D=$STAGE/node
mkdir -p "$D/outpost" "$D/lib" "$D/mesh" "$D/wifi" "$D/net"

if [ "$HALOW" = 1 ]; then
    echo "== building the HaLow kit"
    "$HERE/halow/make-kit.sh" "$D/halow-kit" "" "$ENV_FILE"
fi

echo "== packing Outpost"
# From a git checkout: the tracked (and new, not ignored) files. From a
# downloaded copy (bootstrap.sh): the same folders, without caches.
(cd "$REPO" && if [ -d .git ] && command -v git >/dev/null; then
        git ls-files -co --exclude-standard -- outpost scripts config/*.example.yaml \
            pyproject.toml requirements*.txt README.md
    else
        find outpost scripts config/*.example.yaml pyproject.toml requirements*.txt README.md -type f
    fi | grep -v '__pycache__' |
    tar ${TARFLAGS[@]+"${TARFLAGS[@]}"} -cf - -T -) | tar -C "$D/outpost" -xf -
cp "$HERE/setup-outpost.sh" "$HERE/remote-run.sh" "$D/"
cp "$HERE/lib/pkg.sh" "$D/lib/"
cp "$HERE/wifi/outpost-wifi" "$D/wifi/"
mkdir -p "$D/bt" && cp "$HERE"/bt/outpost-bt-watchdog "$HERE"/bt/outpost-bt-watchdog.service "$HERE"/bt/outpost-bt-watchdog.timer "$D/bt/"
cp "$HERE"/net/outpost-net "$HERE"/net/outpost-net*.service "$HERE"/net/outpost-net-route.timer "$HERE"/net/setup-net.sh "$D/net/"
cp "$HERE/mesh/outpost-mesh" "$HERE/mesh/outpost-mesh.service" "$HERE/mesh/mesh.env" "$HERE/mesh/setup-mesh.sh" "$D/mesh/"
[ -z "$CONFIG" ] || install -m 0600 "$CONFIG" "$D/outpost.yaml"
[ -z "$MESH_ENV" ] || install -m 0600 "$MESH_ENV" "$D/mesh.env"
# Settings for remote-run.sh, quoted so names with spaces stay whole.
{
    printf 'HALOW=%q\nMESH_ASKED=%q\nMESH_WIFI=%q\nMESH_ETH=%q\n' "$HALOW" "$MESH_ASKED" "$MESH_WIFI" "$MESH_ETH"
    printf 'FORCE_MESH=%q\nMESH_SELECT=%q\nNODE_NAME=%q\nUSB_BT=%q\n' "$FORCE_MESH" "$MESH_SELECT" "$NAME" "$USB_BT"
    printf 'WIFI_SETUP=%q\nNET_SETUP=%q\nAUTOSTART=%q\n' "$WIFI_SETUP" "$NET_SETUP" "$AUTOSTART"
    # Installing on this machine from its own ~/outpost git clone: use the
    # code where it is (setup-outpost.sh checks).
    printf 'SOURCE_DIR=%q\n' "$([ "$LOCAL" = 1 ] && echo "$REPO")"
} > "$D/vars.sh"
if [ "$LOCAL" = 1 ]; then
    bash "$D/remote-run.sh"
else
    tar ${TARFLAGS[@]+"${TARFLAGS[@]}"} -C "$STAGE" -czf "$STAGE/node.tgz" node
    echo "== copying to $HOST"
    scp -q "$STAGE/node.tgz" "$HOST:/tmp/outpost-node.tgz"
    ssh -t "$HOST" 'set -e; umask 077; rm -rf /tmp/outpost-node; mkdir /tmp/outpost-node
        tar -C /tmp/outpost-node -xzf /tmp/outpost-node.tgz; rm -f /tmp/outpost-node.tgz
        rc=0; bash /tmp/outpost-node/node/remote-run.sh || rc=$?
        sudo rm -rf /tmp/outpost-node; exit $rc'
fi

if [ "$REBOOT" = 1 ]; then
    echo "== rebooting $HOST"
    on_target "sudo systemctl reboot" || true
else
    echo "== done. Reboot if this was the first install (new kernel, groups, Bluetooth settings)."
fi
