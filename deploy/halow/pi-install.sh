#!/bin/bash
# Runs ON the Pi (as root): installs the HaLow kit from a kit directory.
#   pi-install.sh <kit-dir>
# Used by install.sh (over SSH) and by sd-card.sh (first boot via cloud-init).
# Kit layout: KERNEL_RELEASE, rootfs/, boot/, files/, optional debs/*.deb,
# optional halow.env (site settings shared by all nodes, incl. mesh password).
# Does not reboot.
set -euo pipefail

K=${1:?usage: pi-install.sh <kit-dir>}
KREL=$(cat "$K/KERNEL_RELEASE")
BOOT=/boot/firmware
F=$K/files

model=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown)
echo "== HaLow kit for kernel $KREL on: $model"
case "$model" in
    *"Raspberry Pi 5"*|*"Raspberry Pi 500"*|*"Compute Module 5"*)
        echo "!! This kit's kernel and overlay are for Pi 3/4 (BCM2835-family GPIO), not a Pi 5. Nothing changed."
        exit 1 ;;
esac

echo "== packages"
if ls "$K"/debs/*.deb >/dev/null 2>&1; then
    # Install only what's missing, so a newer system package is never downgraded.
    need=()
    for deb in "$K"/debs/*.deb; do
        pkg=$(dpkg-deb -f "$deb" Package)
        dpkg -s "$pkg" >/dev/null 2>&1 || need+=("$deb")
    done
    [ ${#need[@]} -gt 0 ] && dpkg -i "${need[@]}" >/dev/null
else
    apt-get install -y -qq batctl iw libnl-genl-3-200 libnl-route-3-200 libusb-1.0-0 >/dev/null
fi
command -v batctl >/dev/null || { echo "!! batctl missing"; exit 1; }

echo "== kernel modules + firmware + tools"
rm -rf "/lib/modules/$KREL"
cp -a "$K/rootfs/lib/modules/$KREL" /lib/modules/
mkdir -p /lib/firmware/morse
cp -a "$K"/rootfs/lib/firmware/morse/. /lib/firmware/morse/
install -m 0755 "$K"/rootfs/usr/local/sbin/* /usr/local/sbin/
install -m 0644 "$K/rootfs/boot/config-$KREL" "/boot/config-$KREL"
depmod "$KREL"

echo "== boot files in $BOOT/morse/"
mkdir -p $BOOT/morse
rm -rf $BOOT/morse/overlays
cp -r "$K"/boot/. $BOOT/morse/
cp $BOOT/cmdline.txt $BOOT/morse/cmdline.txt
mkinitramfs -o $BOOT/morse/initramfs8 "$KREL"

echo "== system config"
install -m 0644 "$F/etc/modprobe.d/morse.conf" /etc/modprobe.d/morse.conf
install -m 0644 "$F/etc/udev/rules.d/70-halow.rules" /etc/udev/rules.d/70-halow.rules
mkdir -p /etc/NetworkManager/conf.d
install -m 0644 "$F/etc/NetworkManager/conf.d/halow-mesh.conf" /etc/NetworkManager/conf.d/halow-mesh.conf
install -m 0755 "$F/usr/local/sbin/halow-mesh" /usr/local/sbin/halow-mesh
install -m 0644 "$F/etc/systemd/system/halow-mesh.service" /etc/systemd/system/halow-mesh.service
mkdir -p /etc/halow-mesh
if [ -f "$K/halow.env" ]; then
    install -m 0600 "$K/halow.env" /etc/halow-mesh/halow.env
    echo "   site settings installed from the kit (shared mesh password)"
elif [ ! -f /etc/halow-mesh/halow.env ]; then
    install -m 0600 "$F/etc/halow-mesh/halow.env" /etc/halow-mesh/halow.env
    pw=$(head -c 18 /dev/urandom | base64 | tr -d '/+=')
    sed -i "s/^MESH_PASSWORD=.*/MESH_PASSWORD=$pw/" /etc/halow-mesh/halow.env
    echo "   new mesh password generated - copy /etc/halow-mesh/halow.env to the other nodes"
fi
# Keep the driver's country in step with the mesh settings.
country=$(sed -n 's/^COUNTRY=//p' /etc/halow-mesh/halow.env)
[ -n "$country" ] && sed -i "s/country=[A-Z][A-Z]/country=$country/" /etc/modprobe.d/morse.conf
grep -q "^batman-adv" /etc/modules 2>/dev/null || echo batman-adv >> /etc/modules
systemctl daemon-reload
systemctl enable halow-mesh.service >/dev/null 2>&1
systemctl reload NetworkManager 2>/dev/null || true
udevadm control --reload

# Last, so a failure above leaves the Pi on its stock kernel.
if ! grep -q "^# --- HaLow" $BOOT/config.txt; then
    cp $BOOT/config.txt $BOOT/config.txt.bak-before-halow
    grep -q "^dtparam=spi=on" $BOOT/config.txt || echo "dtparam=spi=on" >> $BOOT/config.txt
    cat >> $BOOT/config.txt <<'EOF'

[all]
# --- HaLow (Heltec HT-HC01P): boot the Morse-patched kernel from morse/.
# To go back to the stock kernel, delete this block (from any computer).
os_prefix=morse/
dtoverlay=hc01p-spi
# --- end HaLow
EOF
fi

echo "== installed (kernel $KREL). Reboot to start the HaLow mesh."
