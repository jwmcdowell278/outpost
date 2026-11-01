#!/bin/bash
# Publishes the HaLow kit built by build.sh (deploy/halow/out) as a GitHub
# release download, so bootstrap.sh can install HaLow on a fresh Pi without
# the repo. Run on the machine that built it, with the GitHub CLI logged in
# (gh auth login):
#   ./publish-kit.sh [tag]        (default tag: halow-kit-<kernel release>)
# The kit holds no secrets: mesh settings (halow.env) are never included.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=$HERE/out
command -v gh >/dev/null || { echo "needs the GitHub CLI (gh) - e.g. brew install gh" >&2; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "log in first: gh auth login" >&2; exit 1; }
[ -f "$OUT/KERNEL_RELEASE" ] || { echo "no build output - run the build first (README.md)" >&2; exit 1; }
KREL=$(cat "$OUT/KERNEL_RELEASE")
TAG=${1:-halow-kit-$KREL}

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
TARFLAGS=(); if tar --version 2>/dev/null | grep -q bsdtar; then TARFLAGS=(--no-xattrs --no-mac-metadata); fi
# Only what make-kit.sh uses: the kernel release, boot files and rootfs.
COPYFILE_DISABLE=1 tar ${TARFLAGS[@]+"${TARFLAGS[@]}"} -C "$OUT" -czf "$STAGE/halow-kit.tar.gz" KERNEL_RELEASE boot rootfs
echo "== kit: $(du -h "$STAGE/halow-kit.tar.gz" | cut -f1) for kernel $KREL"

if gh release view "$TAG" >/dev/null 2>&1; then
    gh release upload "$TAG" "$STAGE/halow-kit.tar.gz" --clobber
else
    gh release create "$TAG" "$STAGE/halow-kit.tar.gz" --latest \
        --title "HaLow kit ($KREL)" \
        --notes "Prebuilt HaLow kit (Morse Micro kernel $KREL, driver, firmware, tools) for 64-bit Raspberry Pi OS on a Pi 3/4. Used by deploy/bootstrap.sh."
fi
echo "== published: https://github.com/$(gh repo view --json nameWithOwner -q .nameWithOwner)/releases/tag/$TAG"
