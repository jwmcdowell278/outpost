#!/bin/bash
# Installs the HaLow kit built by build.sh onto a running Raspberry Pi over SSH.
#   ./install.sh <ssh-host> [--reboot] [--env <halow.env>]
# Needs passwordless sudo on the Pi. --env installs shared site settings
# (copy /etc/halow-mesh/halow.env from an existing node so the mesh password
# matches); without it, an existing file is kept or a new password generated.
#
# The Morse-patched kernel goes in /boot/firmware/morse/ and is selected by
# an `os_prefix=morse/` line in config.txt; the stock kernel is untouched.
# To go back to stock: delete the "HaLow" block at the end of config.txt.
set -euo pipefail

HOST=${1:?usage: install.sh <ssh-host> [--reboot] [--env <halow.env>]}
shift
REBOOT=; ENV_FILE=
while [ $# -gt 0 ]; do
    case "$1" in
        --reboot) REBOOT=1 ;;
        --env) ENV_FILE=$2; shift ;;
        *) echo "unknown option $1" >&2; exit 2 ;;
    esac
    shift
done
HERE=$(cd "$(dirname "$0")" && pwd)

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
"$HERE/make-kit.sh" "$STAGE/kit" "" "$ENV_FILE"
COPYFILE_DISABLE=1 tar --no-xattrs --no-mac-metadata -C "$STAGE" -czf "$STAGE/kit.tgz" kit

echo "== copying to $HOST"
scp -q "$STAGE/kit.tgz" "$HOST:/tmp/halow-kit.tgz"
ssh "$HOST" 'cd /tmp && rm -rf halow-kit && mkdir halow-kit && tar -C halow-kit -xzf halow-kit.tgz &&
    sudo bash /tmp/halow-kit/kit/pi-install.sh /tmp/halow-kit/kit; rc=$?
    sudo rm -rf /tmp/halow-kit /tmp/halow-kit.tgz; exit $rc'

if [ -n "$REBOOT" ]; then
    echo "== rebooting $HOST"
    ssh "$HOST" "sudo systemctl reboot" || true
fi
