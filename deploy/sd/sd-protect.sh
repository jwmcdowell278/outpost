#!/bin/bash
# Fewer writes to a Raspberry Pi's SD card (deploy/README.md, "SD card"):
# the system log is kept in RAM, and swap goes to compressed RAM (zram)
# instead of a file on the card. Run as root; safe to run again.
#   sd-protect.sh          apply
#   sd-protect.sh undo     put the system log and swap back as they were
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "!! run as root" >&2; exit 1; }
JOURNAL_CONF=/etc/systemd/journald.conf.d/90-outpost-sd.conf
ZRAM_CONF=/etc/default/zramswap

card_swaps() {  # swap files/partitions on the SD card (not zram)
    awk 'NR > 1 && $1 !~ /^\/dev\/zram/ {print $1}' /proc/swaps
}

if [ "${1:-}" = undo ]; then
    rm -f "$JOURNAL_CONF"; systemctl restart systemd-journald
    if systemctl cat dphys-swapfile.service >/dev/null 2>&1; then
        systemctl enable --now dphys-swapfile.service >/dev/null 2>&1 || true
        if systemctl cat zramswap.service >/dev/null 2>&1; then systemctl disable --now zramswap.service >/dev/null 2>&1 || true; fi
    fi
    echo "== SD card settings undone (system log on the card again, swap as before)"
    exit 0
fi

echo "== SD card: system log in RAM"
mkdir -p "$(dirname "$JOURNAL_CONF")"
want=$(printf '%s\n' "# Outpost: keep the system log in RAM so it doesn't wear the SD card." \
    "# Logs from before a reboot are lost. Undo: sd-protect.sh undo" \
    "[Journal]" "Storage=volatile" "RuntimeMaxUse=48M")
if [ "$(cat "$JOURNAL_CONF" 2>/dev/null)" != "$want" ]; then
    printf '%s\n' "$want" > "$JOURNAL_CONF"
    systemctl restart systemd-journald
fi
if [ -d /var/log/journal ]; then
    echo "   the old logs on the card ($(du -sh /var/log/journal 2>/dev/null | cut -f1)) are kept;" \
         "delete them with: sudo rm -rf /var/log/journal"
fi

echo "== SD card: swap"
if systemctl is-enabled --quiet dphys-swapfile.service 2>/dev/null || [ -n "$(card_swaps)" ]; then
    # Compressed RAM swap first, so memory pressure never has nowhere to go.
    if ! grep -q '^/dev/zram' /proc/swaps; then
        if ! systemctl cat zramswap.service >/dev/null 2>&1; then
            if command -v apt-get >/dev/null; then
                apt-get install -y -qq zram-tools </dev/null >/dev/null 2>&1 || true
            fi
        fi
        if systemctl cat zramswap.service >/dev/null 2>&1; then
            printf '%s\n' "# Outpost: swap in compressed RAM instead of on the SD card." \
                "ALGO=zstd" "PERCENT=50" "PRIORITY=100" > "$ZRAM_CONF"
            systemctl enable zramswap.service >/dev/null 2>&1 || true
            systemctl restart zramswap.service || echo "   !! zram swap didn't start"
        fi
    fi
    if grep -q '^/dev/zram' /proc/swaps; then
        if systemctl cat dphys-swapfile.service >/dev/null 2>&1; then
            dphys-swapfile swapoff 2>/dev/null || true
            systemctl disable --now dphys-swapfile.service >/dev/null 2>&1 || true
        fi
        for s in $(card_swaps); do swapoff "$s" 2>/dev/null || true; done
        echo "   swap is in compressed RAM (zram)"
        [ -z "$(card_swaps)" ] || echo "   !! still swapping to the card: $(card_swaps | tr '\n' ' ')"
    else
        echo "   !! couldn't set up zram swap - leaving swap on the card as it is"
    fi
else
    echo "   nothing swaps to the card$(grep -q '^/dev/zram' /proc/swaps && echo ' (swap is zram)')"
fi
