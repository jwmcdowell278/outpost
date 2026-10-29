#!/bin/bash
# Prepares a freshly flashed Raspberry Pi OS SD card to install the HaLow kit
# on its first boot - no network needed.
#   ./sd-card.sh [--env <halow.env>] [<bootfs-path>]     (default /Volumes/bootfs)
# Only the FAT boot partition is touched (macOS can't write the Linux one):
# the kit goes in halow-kit/ and cmdline.txt gets a systemd.run= first-boot
# step (firstboot.sh). That runs very early - before cloud-init - installs
# the kit, removes itself from cmdline.txt, deletes halow-kit/ (it may hold
# the mesh password) and reboots; the Pi then carries on its normal first
# boot on the HaLow kernel. Log, readable on any computer from the boot
# partition: halow-install.log
set -euo pipefail

ENV_FILE=; BOOTFS=/Volumes/bootfs
while [ $# -gt 0 ]; do
    case "$1" in
        --env) ENV_FILE=$2; shift ;;
        *) BOOTFS=$1 ;;
    esac
    shift
done
HERE=$(cd "$(dirname "$0")" && pwd)

[ -f "$BOOTFS/cmdline.txt" ] && [ -f "$BOOTFS/config.txt" ] || { echo "$BOOTFS isn't a Pi boot partition" >&2; exit 1; }

# Debian release of the image, from the compiler that built its kernel.
ver=$( (gunzip -c "$BOOTFS/kernel8.img" 2>/dev/null || cat "$BOOTFS/kernel8.img") | strings | grep -m1 "Linux version" || true)
case "$ver" in
    *gcc-14*|*"gcc (Debian 14"*) REL=trixie ;;
    *gcc-12*|*"gcc (Debian 12"*) REL=bookworm ;;
    *) echo "can't tell the Debian release from: $ver" >&2; exit 1 ;;
esac
echo "== image: $REL ($ver)"

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
"$HERE/make-kit.sh" "$STAGE/halow-kit" "$REL" "$ENV_FILE"

need=$(du -sk "$STAGE/halow-kit" | cut -f1); free=$(df -k "$BOOTFS" | awk 'NR==2{print $4}')
[ "$need" -lt "$((free - 20000))" ] || { echo "not enough room on $BOOTFS" >&2; exit 1; }

echo "== copying kit to $BOOTFS/halow-kit"
rm -rf "$BOOTFS/halow-kit"
COPYFILE_DISABLE=1 cp -RX "$STAGE/halow-kit" "$BOOTFS/"

# Undo the older cloud-init based step, if this card has it.
if [ -f "$BOOTFS/user-data" ] && grep -q "halow-kit" "$BOOTFS/user-data"; then
    echo "== removing the old cloud-init HaLow step from user-data"
    python3 - "$BOOTFS/user-data" <<'PY'
import sys, re
p = sys.argv[1]
s = open(p).read()
s = "\n".join(l for l in s.split("\n") if "halow-kit" not in l)
s = re.sub(r"\n*# HaLow: reboot into the HaLow kernel once first boot is done\npower_state:\n(  .*\n?)+", "\n", s)
open(p, "w").write(s.rstrip("\n") + "\n")
PY
fi

echo "== adding first-boot step to cmdline.txt"
cmd=$(tr -d '\n' < "$BOOTFS/cmdline.txt" | sed -E 's/ ?systemd\.(run|run_success_action|run_failure_action|unit)=[^ ]*//g')
printf '%s systemd.run=/boot/firmware/halow-kit/firstboot.sh systemd.run_success_action=reboot systemd.run_failure_action=reboot systemd.unit=kernel-command-line.target\n' "$cmd" > "$BOOTFS/cmdline.txt"
cat "$BOOTFS/cmdline.txt"
rm -f "$BOOTFS/halow-install.log"
dot_clean -m "$BOOTFS" 2>/dev/null || true
sync
echo "== done. Eject the card, put it in the Pi and power on. It reboots once by itself."
echo "   If HaLow doesn't come up, put the card back in a computer and read halow-install.log."
