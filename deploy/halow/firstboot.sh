#!/bin/bash
# First-boot HaLow install, started from cmdline.txt by sd-card.sh:
#   systemd.run=/boot/firmware/halow-kit/firstboot.sh
#   systemd.run_success_action=reboot systemd.run_failure_action=reboot
#   systemd.unit=kernel-command-line.target
# Runs very early (before cloud-init and without any network), then the Pi
# reboots and continues its normal first boot. The log goes to the boot
# partition so it can be read on any computer: /boot/firmware/halow-install.log
BOOT=/boot/firmware
LOG=$BOOT/halow-install.log
exec >"$LOG" 2>&1
echo "HaLow first-boot install started (uptime $(cut -d' ' -f1 /proc/uptime)s)"

# First, take ourselves out of cmdline.txt so this can never run twice.
sed -i -E 's/ ?systemd\.run=[^ ]*//; s/ ?systemd\.run_success_action=[^ ]*//;
           s/ ?systemd\.run_failure_action=[^ ]*//; s/ ?systemd\.unit=kernel-command-line\.target//' \
    $BOOT/cmdline.txt
echo "cmdline.txt now: $(cat $BOOT/cmdline.txt)"
sync

mount -o remount,rw / 2>/dev/null || true
if bash $BOOT/halow-kit/pi-install.sh $BOOT/halow-kit; then
    echo "RESULT: OK - rebooting into the HaLow kernel"
else
    echo "RESULT: FAILED (exit $?) - the Pi stays on its normal kernel"
fi
# The kit holds the mesh password; don't leave it on the FAT partition.
rm -rf $BOOT/halow-kit
sync
exit 0
