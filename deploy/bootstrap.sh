#!/bin/bash
# Installs an Outpost node on THIS machine, straight from GitHub - no clone
# needed. Run as your normal user (it uses sudo):
#
#   curl -sSL https://raw.githubusercontent.com/jwmcdowell278/outpost/main/deploy/bootstrap.sh | bash
#
# With no options it asks a few questions (node name, new or existing mesh,
# HaLow, Wi-Fi, ...) and installs. Joining an existing mesh copies its keys
# and mesh settings from a node you name, over SSH.
#
# With options it doesn't ask - they're install-node.sh's, e.g.:
#   ... | bash -s -- --config ~/node.yaml --env ~/halow.env --name "Pi 2" --reboot
#
# It puts the code in ~/outpost as a git clone - update it later with
# "git pull" there, or by running this again - and, on a 64-bit Raspberry
# Pi, downloads the prebuilt HaLow kit from the repo's latest GitHub release
# (skipped if there's none), then runs "install-node.sh local". An
# ~/outpost from an older install (a plain copy) is turned into a git clone
# in place; its config, logs and Python environment are kept.
#
# Environment: OUTPOST_REPO (default jwmcdowell278/outpost), OUTPOST_REF
# (branch, default main), OUTPOST_TARBALL (a .tar.gz URL of the code to use
# instead of git, e.g. file:///tmp/outpost.tar.gz for testing - ~/outpost
# is then a plain copy, as before).

TITLE="Outpost setup"

# ------------------------------------------------------------ asking helpers
# whiptail where there is one (Raspberry Pi OS, Debian, Ubuntu...), plain
# prompts otherwise.

use_whiptail() { command -v whiptail >/dev/null && [ -t 0 ] && [ "${TERM:-dumb}" != dumb ]; }

say_box() {  # say_box TEXT
    if use_whiptail; then whiptail --title "$TITLE" --msgbox "$1" 20 76; else printf '\n%b\n' "$1"; fi
}

yesno() {  # yesno QUESTION [default yes|no]
    local a def=${2:-yes}
    if use_whiptail; then
        if [ "$def" = no ]; then whiptail --title "$TITLE" --defaultno --yesno "$1" 14 76
        else whiptail --title "$TITLE" --yesno "$1" 14 76; fi
        return
    fi
    while :; do
        printf '\n%b [%s]: ' "$1" "$([ "$def" = yes ] && echo Y/n || echo y/N)"
        read -r a || return 1
        case "${a:-$def}" in [Yy]*) return 0 ;; [Nn]*) return 1 ;; esac
    done
}

ask() {  # ask PROMPT DEFAULT -> answer
    local a
    if use_whiptail; then whiptail --title "$TITLE" --inputbox "$1" 12 76 "$2" 3>&1 1>&2 2>&3; return; fi
    printf '\n%b [%s]: ' "$1" "$2" >&2; read -r a || return 1; echo "${a:-$2}"
}

choose() {  # choose PROMPT TAG LABEL [TAG LABEL...] -> TAG
    local prompt n ans
    prompt=$1; shift
    if use_whiptail; then whiptail --title "$TITLE" --menu "$prompt" 18 76 6 "$@" 3>&1 1>&2 2>&3; return; fi
    local args=("$@")
    while :; do
        printf '\n%b\n' "$prompt" >&2; n=0
        for ((i = 0; i < ${#args[@]}; i += 2)); do n=$((n + 1)); printf '  %d  %s\n' "$n" "${args[i+1]}" >&2; done
        printf 'Number: ' >&2; read -r ans || return 1
        if [[ "$ans" =~ ^[0-9]+$ ]] && [ "$ans" -ge 1 ] && [ "$ans" -le "$n" ]; then
            echo "${args[(ans - 1) * 2]}"; return 0
        fi
    done
}

# ------------------------------------------------- joining an existing mesh

# Copy a root-only file from an existing node: sudo without a password if it
# can (the Raspberry Pi OS default), else with a password prompt over SSH.
fetch_root_file() {  # fetch_root_file HOST REMOTE-PATH LOCAL-PATH
    local host=$1 path=$2 dest=$3
    ssh -o ConnectTimeout=10 "$host" "test -f $path" 2>/dev/null || return 1
    ( umask 077
      if ssh "$host" "sudo -n cat $path" > "$dest" 2>/dev/null && [ -s "$dest" ]; then exit 0; fi
      ssh -t "$host" "sudo install -m 600 -o \$(id -un) $path /tmp/outpost-share.\$\$ && mv /tmp/outpost-share.\$\$ /tmp/outpost-share" &&
          scp -q "$host:/tmp/outpost-share" "$dest" && ssh "$host" "rm -f /tmp/outpost-share" )
}

fetch_from_node() {  # fetch_from_node HOST DIR - fills DIR with what was found
    local host=$1 dir=$2
    echo "== copying the mesh's keys and settings from $host"
    echo "   (you may be asked for $host's password, and to trust its key)"
    if ! ( umask 077; scp -q -o ConnectTimeout=10 "$host:outpost/config/outpost.yaml" "$dir/outpost.yaml" ); then
        echo "!! couldn't copy ~/outpost/config/outpost.yaml from $host" >&2
        return 1
    fi
    echo "   Outpost config: copied"
    if fetch_root_file "$host" /etc/halow-mesh/halow.env "$dir/halow.env"; then
        echo "   HaLow mesh settings: copied"
    else
        rm -f "$dir/halow.env"; echo "   HaLow mesh settings: none on $host"
    fi
    if fetch_root_file "$host" /etc/outpost-mesh/mesh.env "$dir/mesh.env"; then
        echo "   Wi-Fi mesh settings: copied"
    else
        rm -f "$dir/mesh.env"; echo "   Wi-Fi mesh settings: none on $host"
    fi
}

# --------------------------------------------------------------- the wizard

# Fills the global OPTS array with install-node.sh options.
wizard() {
    local tmp=$1 is_pi=$2 have_kit=$3 name how host summary f
    OPTS=()
    say_box "Welcome! This sets up this machine as an Outpost node:\n\n  - Outpost (encrypted push-to-talk), started at boot\n  - the network tools (sudo outpost-net)\n$([ "$have_kit" = 1 ] && echo "  - the HaLow HAT drivers and mesh")\n\nYou'll be asked a few questions first. Nothing is changed until you confirm at the end."

    name=$(ask "Name for this node (shown to other nodes):" "$(cat /proc/sys/kernel/hostname)") || exit 1
    [ -n "$name" ] && OPTS+=(--name "$name")

    how=$(choose "Is this the first node of a new mesh, or should it join an existing one?" \
        join "Join an existing mesh (copy its keys from another node)" \
        new "Start a new mesh (this is the first node - new keys)" \
        files "Join using files I've already copied here") || exit 1
    summary="Node name: $name"
    case "$how" in
        join)
            while :; do
                host=$(ask "An existing node to copy the keys from, as user@address\n(e.g. pi@192.168.1.20 or pi@outpost1.local):" "${host:-}") || exit 1
                [ -n "$host" ] || continue
                mkdir -p "$tmp/join"
                if fetch_from_node "$host" "$tmp/join"; then break; fi
                yesno "Couldn't copy from $host. Try again?" yes || exit 1
            done
            OPTS+=(--config "$tmp/join/outpost.yaml")
            [ -f "$tmp/join/halow.env" ] && OPTS+=(--env "$tmp/join/halow.env")
            [ -f "$tmp/join/mesh.env" ] && OPTS+=(--mesh-env "$tmp/join/mesh.env")
            summary="$summary\nMesh: join, keys copied from $host" ;;
        files)
            f=$(ask "Path to the Outpost config (outpost.yaml):" "$HOME/node.yaml") || exit 1
            [ -f "$f" ] || { echo "$f not found" >&2; exit 1; }
            OPTS+=(--config "$f")
            f=$(ask "Path to the HaLow settings (halow.env), or empty for none:" "$([ -f "$HOME/halow.env" ] && echo "$HOME/halow.env")") || exit 1
            if [ -n "$f" ]; then [ -f "$f" ] || { echo "$f not found" >&2; exit 1; }; OPTS+=(--env "$f"); fi
            f=$(ask "Path to the Wi-Fi mesh settings (mesh.env), or empty for none:" "") || exit 1
            if [ -n "$f" ]; then [ -f "$f" ] || { echo "$f not found" >&2; exit 1; }; OPTS+=(--mesh-env "$f"); fi
            summary="$summary\nMesh: join, using files on this machine" ;;
        new)
            summary="$summary\nMesh: NEW (new keys - copy this node's config to the others)" ;;
    esac

    if [ "$have_kit" = 1 ]; then
        if yesno "Install the HaLow HAT drivers (Heltec HT-HC01P) and HaLow mesh?" yes; then
            summary="$summary\nHaLow: yes"
        else
            OPTS+=(--no-halow); summary="$summary\nHaLow: no"
        fi
    elif [ "$is_pi" = 1 ]; then
        summary="$summary\nHaLow: not available (no HaLow kit published, or not 64-bit)"
    fi
    if [ "$is_pi" = 1 ] && yesno "Does this Pi use a USB Bluetooth adapter for the headset?\n(Recommended on a Pi 3 - the built-in Bluetooth is then turned off.)" no; then
        OPTS+=(--usb-bluetooth); summary="$summary\nBluetooth: USB adapter (built-in turned off)"
    fi
    if yesno "Choose Wi-Fi or Ethernet cards for the mesh now?\n(You can do it later: sudo outpost-net, Mesh interfaces.)" no; then
        OPTS+=(--mesh-select); summary="$summary\nMesh cards: choose during the install"
    fi
    if command -v nmcli >/dev/null && yesno "Set up Wi-Fi now (join a network, or be an access point)?\n(Later: sudo outpost-net, Wi-Fi.)" no; then
        OPTS+=(--wifi-setup); summary="$summary\nWi-Fi: set up during the install"
    fi
    if yesno "Set up gateways or forwarding now (share internet with the mesh)?\n(Later: sudo outpost-net.)" no; then
        OPTS+=(--net-setup); summary="$summary\nGateways: set up during the install"
    fi
    if yesno "Start Outpost automatically at boot?" yes; then
        summary="$summary\nStart at boot: yes"
    else
        OPTS+=(--no-autostart); summary="$summary\nStart at boot: no"
    fi
    if yesno "Reboot when the install is done? (Needed after the first install.)" yes; then
        OPTS+=(--reboot); summary="$summary\nReboot at the end: yes"
    fi
    yesno "Ready to install:\n\n$summary\n\nGo ahead?" yes || { echo "Cancelled - nothing was changed."; exit 0; }
}

# ------------------------------------------------------------------- the code

# git, installed if it's missing. Fails quietly if it can't be.
ensure_git() {
    command -v git >/dev/null && return 0
    echo "== installing git"
    if command -v apt-get >/dev/null; then
        sudo apt-get update -qq </dev/null >/dev/null && sudo apt-get install -y -qq git </dev/null >/dev/null
    elif command -v dnf >/dev/null; then sudo dnf install -y -q git </dev/null
    elif command -v pacman >/dev/null; then sudo pacman -S --noconfirm --needed git </dev/null
    elif command -v zypper >/dev/null; then sudo zypper -n -q install git </dev/null
    fi
    command -v git >/dev/null
}

# Make DIR a git clone of URL on branch REF, up to date with GitHub.
checkout_code() {  # checkout_code URL REF DIR
    local url=$1 ref=$2 dir=$3
    if [ -d "$dir/.git" ]; then
        echo "== updating the Outpost code in $dir (git pull)"
        git -C "$dir" fetch -q origin "$ref"
        if ! git -C "$dir" merge -q --ff-only FETCH_HEAD; then
            echo "!! $dir has changes of its own that clash with $ref on GitHub." >&2
            echo "   See them with: git -C $dir status - then run this again." >&2
            exit 1
        fi
    elif [ -e "$dir" ]; then
        # From an install before ~/outpost was a git clone. The checkout
        # replaces the code; config/, logs and .venv aren't in git (see
        # .gitignore), so they stay as they are.
        echo "== turning $dir into a git clone (config, logs and the Python environment are kept)"
        git -C "$dir" init -q
        git -C "$dir" remote add origin "$url" 2>/dev/null || git -C "$dir" remote set-url origin "$url"
        git -C "$dir" fetch -q origin "+refs/heads/${ref}:refs/remotes/origin/${ref}"
        git -C "$dir" checkout -q -f -B "$ref" "origin/$ref"
        git -C "$dir" branch -q --set-upstream-to="origin/$ref" "$ref"
    else
        echo "== downloading Outpost (git clone $url)"
        git clone -q --branch "$ref" "$url" "$dir"
    fi
    echo "   $(git -C "$dir" log -1 --format='%h %s (%cd)' --date=short)"
}

# ---------------------------------------------------------------------- main

# Everything runs from main(), so bash has read the whole script before it
# runs - needed when it's piped from curl, since main() then points the
# terminal at stdin for sudo, ssh and the questions.
main() {
    set -euo pipefail
    local repo ref src tmp kit_url code is_pi=0 have_kit=0
    repo=${OUTPOST_REPO:-jwmcdowell278/outpost}
    ref=${OUTPOST_REF:-main}

    if [ "$(id -u)" = 0 ]; then
        echo "Run this as your normal user (the one Outpost will run as), not root - it uses sudo itself." >&2
        exit 1
    fi
    for t in curl tar sudo; do
        command -v "$t" >/dev/null || { echo "needs $t - install it first" >&2; exit 1; }
    done
    # Piped from curl: read answers from the terminal.
    if [ ! -t 0 ] && (exec </dev/tty) 2>/dev/null; then exec </dev/tty; fi

    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    if [ -z "${OUTPOST_TARBALL:-}" ] && ensure_git; then
        code=$HOME/outpost
        checkout_code "https://github.com/$repo.git" "$ref" "$code"
    else
        [ -n "${OUTPOST_TARBALL:-}" ] || echo "   couldn't install git - using a plain copy of the code (no git pull)"
        code=$tmp
        src=${OUTPOST_TARBALL:-https://codeload.github.com/$repo/tar.gz/$ref}
        echo "== downloading Outpost ($src)"
        curl -fsSL "$src" | tar -xz -C "$tmp" --strip-components=1
    fi

    # The HaLow kit (kernel + driver, built with deploy/halow/build.sh) is too
    # big for the repo; it's published as a release download.
    tr -d '\0' 2>/dev/null < /proc/device-tree/model | grep -q "Raspberry Pi" && is_pi=1
    if [ "$is_pi" = 1 ] && [ "$(uname -m)" = aarch64 ] && ! printf '%s\n' "$@" | grep -qx -- --no-halow; then
        kit_url="https://github.com/$repo/releases/latest/download/halow-kit.tar.gz"
        echo "== downloading the HaLow kit"
        # deploy/halow/out/ is git-ignored, so the kit doesn't touch the clone.
        rm -rf "$code/deploy/halow/out"
        mkdir -p "$code/deploy/halow/out"
        if curl -fsSL "$kit_url" | tar -xz -C "$code/deploy/halow/out"; then
            echo "   HaLow kit for kernel $(cat "$code/deploy/halow/out/KERNEL_RELEASE")"; have_kit=1
        else
            echo "   no HaLow kit published at $kit_url - HaLow will be skipped"
            rm -rf "$code/deploy/halow/out"
        fi
    fi

    if [ $# -eq 0 ] && [ -t 0 ]; then
        # No options: ask. whiptail makes it nicer; get it if it's missing.
        if ! command -v whiptail >/dev/null; then
            # </dev/null: the package manager mustn't read the answers being typed.
            ( . "$code/deploy/lib/pkg.sh" && pkg_detect && pkg_want menu ) </dev/null >/dev/null 2>&1 || true
        fi
        wizard "$tmp" "$is_pi" "$have_kit"   # its working folder: deleted at exit (holds copied keys)
        set -- ${OPTS[@]+"${OPTS[@]}"}
    fi

    bash "$code/deploy/install-node.sh" local "$@"
    exit $?
}

main "$@"
