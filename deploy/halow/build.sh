#!/bin/bash
# Builds the Morse Micro HaLow stack for a 64-bit Raspberry Pi 3/4 running
# Raspberry Pi OS Bookworm: patched kernel + modules, morse driver, firmware,
# wpa_supplicant_s1g/hostapd_s1g and morse_cli. Runs inside the container from
# Dockerfile (see README.md). Sources are built in /work (a Docker volume - the
# kernel tree needs a case-sensitive filesystem); results go to /out.
set -euo pipefail

MORSE_VER=${MORSE_VER:-1.15.3}            # driver/firmware/userspace release
KERNEL_BRANCH=${KERNEL_BRANCH:-mm/rpi-6.6.31/1.15.x}
FW_BRANCH=${FW_BRANCH:-1.15}
LOCALVER=${LOCALVER:--v8-morse}
JOBS=$(nproc)

W=/work; O=/out
mkdir -p "$W" "$O"

clone() {  # clone <repo> <ref> <dir>
    [ -d "$W/$3/.git" ] || git clone --depth 1 --branch "$2" --recurse-submodules --shallow-submodules \
        "https://github.com/MorseMicro/$1.git" "$W/$3"
}

echo "== kernel ($KERNEL_BRANCH)"
clone rpi-linux "$KERNEL_BRANCH" linux
cd "$W/linux"
if [ ! -f .config ]; then
    make bcm2711_defconfig
    ./scripts/config --set-str LOCALVERSION "$LOCALVER" \
        --module BATMAN_ADV --enable BATMAN_ADV_BATMAN_V --enable BATMAN_ADV_BLA \
        --enable BATMAN_ADV_DAT --enable BATMAN_ADV_MCAST \
        --enable MAC80211_MESH
    make olddefconfig
fi
make -j"$JOBS" Image modules dtbs
KREL=$(make -s kernelrelease)
echo "kernel release: $KREL"

rm -rf "$O/rootfs" "$O/boot"
mkdir -p "$O/rootfs" "$O/boot/overlays"
make INSTALL_MOD_PATH="$O/rootfs" INSTALL_MOD_STRIP=1 modules_install
mkdir -p "$O/rootfs/boot"
cp .config "$O/rootfs/boot/config-$KREL"   # mkinitramfs on the Pi reads this
rm -f "$O/rootfs/lib/modules/$KREL/build" "$O/rootfs/lib/modules/$KREL/source"
cp arch/arm64/boot/Image "$O/boot/kernel8.img"
cp arch/arm64/boot/dts/broadcom/bcm2710-*.dtb arch/arm64/boot/dts/broadcom/bcm2711-*.dtb "$O/boot/"
cp arch/arm64/boot/dts/overlays/*.dtbo "$O/boot/overlays/"
cp arch/arm64/boot/dts/overlays/README "$O/boot/overlays/" 2>/dev/null || true

echo "== morse driver ($MORSE_VER)"
clone morse_driver "$MORSE_VER" morse_driver
cd "$W/morse_driver"
make -j"$JOBS" KERNEL_SRC="$W/linux" CONFIG_WLAN_VENDOR_MORSE=m CONFIG_MORSE_SPI=y \
    CONFIG_MORSE_USER_ACCESS=y CONFIG_MORSE_VENDOR_COMMAND=y
make KERNEL_SRC="$W/linux" INSTALL_MOD_PATH="$O/rootfs" INSTALL_MOD_STRIP=1 modules_install
depmod -b "$O/rootfs" "$KREL"

echo "== firmware ($FW_BRANCH)"
clone morse-firmware "$FW_BRANCH" morse-firmware
cd "$W/morse-firmware"
make TARGET_DIR="$O/rootfs/lib/firmware/morse" install

echo "== Heltec HT-HC01P board config file"
BCF_URL="https://resource.heltec.cn/download/HT-HC01P/BCF/driver_1_15_3/bcf_HC0P.bin"
BCF_SHA256=57c50cb2c1d51187667677b392684285c616ddbf3fe262c3aeece342f446773e
curl -fsSL -o "$O/rootfs/lib/firmware/morse/bcf_HC0P.bin" "$BCF_URL"
echo "$BCF_SHA256  $O/rootfs/lib/firmware/morse/bcf_HC0P.bin" | sha256sum -c --quiet

echo "== hostap ($MORSE_VER)"
clone hostap "$MORSE_VER" hostap
for d in wpa_supplicant hostapd; do
    cd "$W/hostap/$d"
    [ -f .config ] || cp defconfig .config
    # S1G (HaLow), mesh and SAE support
    for opt in CONFIG_MESH CONFIG_SAE CONFIG_IEEE80211AC CONFIG_IEEE80211N CONFIG_IEEE80211AH \
               CONFIG_CTRL_IFACE CONFIG_DEBUG_FILE; do
        grep -q "^$opt=y" .config || echo "$opt=y" >> .config
    done
    # Bookworm ships OpenSSL 3, whose deprecation warnings hostap builds as errors
    CFLAGS="-O2 -Wno-error=deprecated-declarations" make -j"$JOBS"
done
mkdir -p "$O/rootfs/usr/local/sbin"
for b in wpa_supplicant/wpa_supplicant_s1g wpa_supplicant/wpa_cli_s1g hostapd/hostapd_s1g hostapd/hostapd_cli_s1g; do
    cp "$W/hostap/$b" "$O/rootfs/usr/local/sbin/"
done

echo "== morse_cli ($MORSE_VER)"
clone morse_cli "$MORSE_VER" morse_cli
cd "$W/morse_cli"
make clean >/dev/null 2>&1 || true
# nl80211 transport = talk to the driver (wpa_supplicant_s1g calls morse_cli)
make -j"$JOBS" CONFIG_MORSE_TRANS_NL80211=1
cp morse_cli "$O/rootfs/usr/local/sbin/"

echo "== HC01P overlay"
dtc -@ -q -I dts -O dtb -o "$O/boot/overlays/hc01p-spi.dtbo" /src/hc01p-spi-overlay.dts

echo "$KREL" > "$O/KERNEL_RELEASE"
echo "== done: $KREL"
