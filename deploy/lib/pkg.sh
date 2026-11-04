# Package helpers for setup-outpost.sh and setup-mesh.sh (sourced, bash).
# Supports apt (Debian, Ubuntu, Raspberry Pi OS), dnf (Fedora, RHEL-likes),
# pacman (Arch, Manjaro) and zypper (openSUSE).
#   pkg_detect                      sets PM, or fails with a list of what to install
#   pkg_names <role>                package names for a role on this system
#   pkg_need <role>...              install; fail if any package is missing
#   pkg_want <role>...              install what's available; warn about the rest
# Uses sudo unless already root.

SUDO=; [ "$(id -u)" = 0 ] || SUDO=sudo
PM=; _PKG_UPDATED=

pkg_detect() {
    local pm
    for pm in apt-get dnf zypper pacman; do
        command -v "$pm" >/dev/null && { PM=$pm; return 0; }
    done
    echo "!! no supported package manager (apt, dnf, zypper, pacman). Install by hand:" >&2
    echo "   Python 3.10+ with venv and headers, a C compiler, libopus, PortAudio," >&2
    echo "   hidapi, BlueZ, PipeWire (with Bluetooth), espeak-ng, setcap; for the mesh" >&2
    echo "   batctl, iw and wpa_supplicant. Then run this again with SKIP_PACKAGES=1." >&2
    return 1
}

pkg_names() {
    case "$PM:$1" in
        apt-get:build)    echo git python3 python3-venv python3-dev build-essential pkg-config libffi-dev libusb-1.0-0-dev libudev-dev ;;
        apt-get:runtime)  echo libopus0 libportaudio2 libhidapi-hidraw0 libcap2-bin rfkill kmod ;;
        apt-get:bluetooth) echo bluez usbutils ;;
        apt-get:pipewire) echo pipewire pipewire-pulse wireplumber ;;
        apt-get:audio-extra) echo libspa-0.2-bluetooth pulseaudio-utils libasound2-plugins pipewire-alsa ;;
        apt-get:tts)      echo espeak-ng ;;
        apt-get:mesh)     echo batctl iw wpasupplicant iproute2 ;;
        apt-get:menu)     echo whiptail ;;
        apt-get:firewall) echo nftables iproute2 ;;
        apt-get:dns)  echo dnsmasq-base ;;
        apt-get:nettools)   echo iputils-arping iputils-ping ;;
        apt-get:time)       echo chrony ;;

        dnf:build)        echo git python3 python3-devel gcc gcc-c++ make pkgconf-pkg-config libffi-devel libusb1-devel systemd-devel ;;
        dnf:runtime)      echo opus portaudio hidapi libcap util-linux kmod ;;
        dnf:bluetooth)    echo bluez usbutils ;;
        dnf:pipewire)     echo pipewire pipewire-pulseaudio wireplumber ;;
        dnf:audio-extra)  echo pulseaudio-utils alsa-plugins-pulseaudio ;;
        dnf:tts)          echo espeak-ng ;;
        dnf:mesh)         echo batctl iw wpa_supplicant kernel-modules-extra iproute ;;
        dnf:menu)     echo newt ;;
        dnf:firewall) echo nftables iproute ;;
        dnf:dns)      echo dnsmasq ;;
        dnf:nettools)       echo iputils ;;
        dnf:time)           echo chrony ;;

        pacman:build)     echo git python base-devel pkgconf libffi libusb systemd-libs ;;
        pacman:runtime)   echo opus portaudio hidapi libcap util-linux kmod ;;
        pacman:bluetooth) echo bluez bluez-utils usbutils ;;
        pacman:pipewire)  echo pipewire pipewire-pulse wireplumber ;;
        pacman:audio-extra) echo pipewire-audio libpulse ;;
        pacman:tts)       echo espeak-ng ;;
        pacman:mesh)      echo batctl iw wpa_supplicant iproute2 ;;
        pacman:menu)     echo libnewt ;;
        pacman:firewall) echo nftables iproute2 ;;
        pacman:dns)   echo dnsmasq ;;
        pacman:nettools)    echo iputils ;;
        pacman:time)        echo chrony ;;

        zypper:build)     echo git python3 python3-devel gcc gcc-c++ make pkg-config libffi-devel libusb-1_0-devel systemd-devel ;;
        zypper:runtime)   echo libopus0 libportaudio2 libhidapi-hidraw0 libcap-progs util-linux kmod ;;
        zypper:bluetooth) echo bluez usbutils ;;
        zypper:pipewire)  echo pipewire pipewire-pulseaudio wireplumber ;;
        zypper:audio-extra) echo pipewire-spa-plugins-0_2 pulseaudio-utils ;;
        zypper:tts)       echo espeak-ng ;;
        zypper:mesh)      echo batctl iw wpa_supplicant iproute2 ;;
        zypper:menu)     echo newt ;;
        zypper:firewall) echo nftables iproute2 ;;
        zypper:dns)   echo dnsmasq ;;
        zypper:nettools)    echo iputils ;;
        zypper:time)        echo chrony ;;
        *) return 1 ;;
    esac
}

_pkg_update() {
    [ -z "$_PKG_UPDATED" ] || return 0
    _PKG_UPDATED=1
    case "$PM" in
        apt-get) $SUDO apt-get update -qq ;;
        zypper)  $SUDO zypper --non-interactive --quiet refresh ;;
        *) : ;;   # dnf refreshes by itself; pacman: see _pkg_install
    esac
}

_pkg_install() {  # _pkg_install <pkg>... - true if all installed
    case "$PM" in
        apt-get) $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "$@" >/dev/null ;;
        dnf)     $SUDO dnf install -y -q "$@" >/dev/null ;;
        zypper)  $SUDO zypper --non-interactive --quiet install --no-recommends "$@" >/dev/null ;;
        # No -Sy here: refreshing without a full upgrade can break an Arch system.
        pacman)  $SUDO pacman -S --needed --noconfirm "$@" >/dev/null ;;
    esac
}

pkg_need() {
    local pkgs=() r
    for r in "$@"; do read -ra p <<<"$(pkg_names "$r")"; pkgs+=("${p[@]}"); done
    [ ${#pkgs[@]} -gt 0 ] || return 0
    _pkg_update
    if ! _pkg_install "${pkgs[@]}"; then
        echo "!! couldn't install: ${pkgs[*]}" >&2
        [ "$PM" = pacman ] && echo "   On Arch, update the system first (sudo pacman -Syu) and run this again." >&2
        return 1
    fi
}

pkg_want() {
    local pkgs=() r p missing=()
    for r in "$@"; do read -ra p <<<"$(pkg_names "$r")"; pkgs+=("${p[@]}"); done
    [ ${#pkgs[@]} -gt 0 ] || return 0
    _pkg_update
    _pkg_install "${pkgs[@]}" 2>/dev/null && return 0
    # Names differ between releases - install one by one and report the rest.
    for p in "${pkgs[@]}"; do _pkg_install "$p" 2>/dev/null || missing+=("$p"); done
    [ ${#missing[@]} -eq 0 ] || echo "   (not available here, skipped: ${missing[*]})"
}
