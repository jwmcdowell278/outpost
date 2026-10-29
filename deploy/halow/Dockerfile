# Build environment for the HaLow kernel/driver kit. Bookworm arm64 so the
# compiler and glibc match Raspberry Pi OS Bookworm (64-bit). On an Apple
# Silicon Mac or any arm64 host this builds natively.
FROM debian:bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential bc bison flex libssl-dev libelf-dev git kmod cpio \
        device-tree-compiler python3 rsync ca-certificates pkg-config curl \
        libnl-3-dev libnl-genl-3-dev libnl-route-3-dev libusb-1.0-0-dev \
    && rm -rf /var/lib/apt/lists/*
# build.sh and the overlay come from the bind-mounted /src (this folder)
ENTRYPOINT ["/bin/bash", "/src/build.sh"]
