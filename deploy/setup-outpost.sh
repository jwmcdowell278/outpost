#!/bin/bash
# Runs ON the target machine as the user Outpost runs as (not root; uses sudo
# where needed). Started by install-node.sh:
#   setup-outpost.sh <node-dir>
# node-dir holds outpost/ (the code), lib/pkg.sh and, optionally,
# outpost.yaml (the shared config). Env: NODE_NAME (default: hostname),
# USB_BT=1 to turn off a Raspberry Pi's built-in Bluetooth, AUTOSTART=0 to
# not start Outpost at boot, SKIP_PACKAGES=1
# if the system packages are already installed by hand.
# Works on any systemd Linux with apt, dnf, pacman or zypper. Safe to run
# again to update.
set -euo pipefail

N=${1:?usage: setup-outpost.sh <node-dir>}
[ "$(id -u)" != 0 ] || { echo "!! run as the normal user, not root" >&2; exit 1; }
. "$N/lib/pkg.sh"
APP=$HOME/outpost
NAME=${NODE_NAME:-$(cat /proc/sys/kernel/hostname)}
ME=$(id -un)
command -v systemctl >/dev/null || { echo "!! this system doesn't use systemd - Outpost's service needs it" >&2; exit 1; }

if [ "${SKIP_PACKAGES:-0}" != 1 ]; then
    echo "== system packages"
    pkg_detect
    pkg_need build runtime
    pkg_want bluetooth tts menu
    # Bluetooth headset audio needs PipeWire. Don't swap out a sound server
    # that's already there (e.g. PulseAudio on an older desktop).
    if command -v pipewire >/dev/null || ! command -v pulseaudio >/dev/null; then
        pkg_want pipewire audio-extra
    else
        echo "   PulseAudio is in use - leaving it; Bluetooth headset audio may need PipeWire"
    fi
fi

# Python 3.10 or newer, with venv.
PY=
for c in python3 python3.13 python3.12 python3.11 python3.10 python3.14; do
    p=$(command -v "$c" 2>/dev/null) || continue
    "$p" -c 'import sys, venv, ensurepip; sys.exit(sys.version_info < (3, 10))' 2>/dev/null && { PY=$p; break; }
done
[ -n "$PY" ] || { echo "!! needs Python 3.10 or newer with venv" >&2; exit 1; }
PYV=$("$PY" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
echo "== Python $PYV ($PY)"

echo "== Outpost code in $APP"
mkdir -p "$APP"
# Replace the code but keep config/, logs and the virtual environment.
rm -rf "$APP/outpost" "$APP/scripts"
cp -a "$N"/outpost/. "$APP/"

echo "== Python environment"
if [ -x "$APP/.venv/bin/python" ] && ! "$APP/.venv/bin/python" -c 'import sys' 2>/dev/null; then
    rm -rf "$APP/.venv"          # broken (e.g. the system Python was upgraded)
fi
if [ -x "$APP/.venv/bin/python" ] && [ "$("$APP/.venv/bin/python" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')" != "$PYV" ]; then
    rm -rf "$APP/.venv"          # made with another Python version
fi
[ -x "$APP/.venv/bin/python" ] || "$PY" -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q -e "${APP}[web,hardware]"

# Bluetooth headset buttons (the Inrico's PTT) are read from the kernel's
# Bluetooth monitor, which needs CAP_NET_RAW. Give it to a private copy of
# Python in the venv only (README: "Bluetooth headset buttons").
if command -v setcap >/dev/null || [ -x /usr/sbin/setcap ] || [ -x /sbin/setcap ]; then
    REAL=$(readlink -f "$PY")
    cp --remove-destination "$REAL" "$APP/.venv/bin/python$PYV"
    ln -sf "python$PYV" "$APP/.venv/bin/python3"
    ln -sf "python$PYV" "$APP/.venv/bin/python"
    $SUDO env PATH="$PATH:/usr/sbin:/sbin" setcap cap_net_raw+ep "$APP/.venv/bin/python$PYV" \
        || echo "   couldn't set CAP_NET_RAW - Bluetooth headset buttons won't work"
else
    echo "   setcap not found - Bluetooth headset buttons won't work"
fi

echo "== config"
mkdir -p "$APP/config"
CFG=$APP/config/outpost.yaml
if [ -f "$N/outpost.yaml" ]; then
    [ ! -f "$CFG" ] || cp -p "$CFG" "$CFG.bak-$(date +%Y%m%d-%H%M%S)"
    install -m 0600 "$N/outpost.yaml" "$CFG"
    # Per-node values: its own name, and never another node's sender_id.
    sed -i -E '/^sender_id:/d; /^sender_alias:/d' "$CFG"
    printf 'sender_alias: "%s"\n' "$NAME" >> "$CFG"
    echo "   installed the shared config as node \"$NAME\""
elif [ ! -f "$CFG" ]; then
    echo "!! no config here - run install-node.sh again with --config" >&2
    exit 1
else
    echo "   keeping the existing config"
fi
chmod 0600 "$CFG"

echo "== permissions"
for g in input audio bluetooth dialout uucp gpio; do
    if getent group "$g" >/dev/null; then $SUDO usermod -aG "$g" "$ME"; fi
done

echo "== Bluetooth"
command -v rfkill >/dev/null && { $SUDO rfkill unblock bluetooth 2>/dev/null || true; }
command -v systemctl >/dev/null && { $SUDO systemctl enable --now bluetooth.service >/dev/null 2>&1 || true; }
if [ "${USB_BT:-0}" = 1 ]; then
    BOOTCFG=; for f in /boot/firmware/config.txt /boot/config.txt; do [ -f "$f" ] && { BOOTCFG=$f; break; }; done
    if [ -z "$BOOTCFG" ]; then
        echo "   --usb-bluetooth is for a Raspberry Pi; no config.txt here, skipped"
    elif ! grep -q '^dtoverlay=disable-bt' "$BOOTCFG"; then
        echo 'dtoverlay=disable-bt' | $SUDO tee -a "$BOOTCFG" >/dev/null
        echo "   built-in Bluetooth off from the next boot (use a USB adapter)"
    fi
fi
# Keep Bluetooth audio running with nobody logged in. WirePlumber 0.4 reads
# the .lua file, 0.5 the .conf; each ignores the other.
mkdir -p "$HOME/.config/wireplumber/bluetooth.lua.d" "$HOME/.config/wireplumber/wireplumber.conf.d"
echo 'bluez_monitor.properties["with-logind"] = false' > "$HOME/.config/wireplumber/bluetooth.lua.d/80-headless.lua"
cat > "$HOME/.config/wireplumber/wireplumber.conf.d/80-headless.conf" <<'EOF'
# Keep Bluetooth audio running when nobody is logged in (headless node).
wireplumber.profiles = {
  main = {
    monitor.bluez.seat-monitoring = disabled
  }
}
EOF

echo "== service"
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/outpost.service" <<'EOF'
[Unit]
Description=Outpost push-to-talk voice client
# Needs the user's audio system (PipeWire) for the Bluetooth headset.
Wants=pipewire.service wireplumber.service pipewire-pulse.service
After=pipewire.service wireplumber.service pipewire-pulse.service

[Service]
WorkingDirectory=%h/outpost
ExecStart=%h/outpost/.venv/bin/python scripts/run_client.py --config config/outpost.yaml
Restart=always
RestartSec=5
TimeoutStopSec=10

[Install]
WantedBy=default.target
EOF
$SUDO loginctl enable-linger "$ME" || echo "!! couldn't enable lingering - Outpost would only start after a login"
export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
if [ "${AUTOSTART:-1}" = 0 ]; then
    if systemctl --user daemon-reload 2>/dev/null; then
        systemctl --user disable outpost.service >/dev/null 2>&1 || true
        systemctl --user restart wireplumber.service 2>/dev/null || true
        # Already running (by hand): restart it on the new code, but leave it off at boot.
        if systemctl --user is-active --quiet outpost.service; then systemctl --user restart outpost.service; fi
    else
        rm -f "$HOME/.config/systemd/user/default.target.wants/outpost.service"
    fi
    echo "   outpost.service installed but NOT started at boot"
    echo "   start it now:      systemctl --user start outpost"
    echo "   start it at boot:  systemctl --user enable --now outpost"
elif systemctl --user daemon-reload 2>/dev/null; then
    systemctl --user enable outpost.service >/dev/null 2>&1
    systemctl --user restart wireplumber.service 2>/dev/null || true
    systemctl --user restart outpost.service
    echo "   outpost.service enabled and (re)started"
else
    # No user session yet: enable by link; it starts at the next boot.
    mkdir -p "$HOME/.config/systemd/user/default.target.wants"
    ln -sf ../outpost.service "$HOME/.config/systemd/user/default.target.wants/outpost.service"
    echo "   outpost.service enabled; starts at next boot"
fi
alias=$(sed -n -E 's/^sender_alias: *"?([^"]*)"?$/\1/p' "$CFG" | tail -1)
echo "== Outpost installed for $ME (node \"${alias:-$NAME}\")"
