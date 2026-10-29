#!/bin/bash
# Assembles an install kit from the build output.
#   make-kit.sh <dest-dir> [debian-release] [halow.env]
# debian-release (bookworm|trixie): bundle that release's arm64 packages
# (batctl, iw, libs) so the Pi needs no network. Empty = Pi uses apt.
set -euo pipefail

DEST=${1:?usage: make-kit.sh <dest-dir> [debian-release] [halow.env]}
REL=${2:-}
ENV_FILE=${3:-}
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=$HERE/out

[ -f "$OUT/KERNEL_RELEASE" ] || { echo "no build output - run the build first (README.md)" >&2; exit 1; }
[ -f "$OUT/rootfs/lib/firmware/morse/bcf_HC0P.bin" ] || { echo "build output lacks bcf_HC0P.bin - rebuild" >&2; exit 1; }

rm -rf "$DEST"; mkdir -p "$DEST"
cp -R "$OUT/rootfs" "$OUT/boot" "$HERE/files" "$DEST/"
cp "$OUT/KERNEL_RELEASE" "$DEST/"
cp "$HERE/pi-install.sh" "$HERE/firstboot.sh" "$DEST/"
[ -n "$ENV_FILE" ] && install -m 0600 "$ENV_FILE" "$DEST/halow.env"

if [ -n "$REL" ]; then
    echo "== downloading $REL arm64 packages"
    mkdir -p "$DEST/debs"
    docker run --rm --platform linux/arm64 -v "$DEST/debs:/debs" "debian:$REL" bash -c \
        'apt-get -qq update >/dev/null && cd /debs && apt-get -qq download \
         batctl iw libnl-3-200 libnl-genl-3-200 libnl-route-3-200 libusb-1.0-0 >/dev/null && ls'
fi
echo "== kit ready in $DEST ($(du -sh "$DEST" | cut -f1))"
