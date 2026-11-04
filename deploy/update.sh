#!/bin/bash
# Updates Outpost on this machine from GitHub, as the user it runs as:
#   ~/outpost/deploy/update.sh
# git pull, then the Python packages if they changed, then restarts Outpost.
# Config, logs and keys aren't in git, so they're kept. For new system
# packages, HaLow drivers or network tools, run bootstrap.sh again instead
# (it also does a git pull).
set -euo pipefail
APP=$(cd "$(dirname "$0")/.." && pwd)
[ "$(id -u)" != 0 ] || { echo "!! run as the normal user Outpost runs as, not root" >&2; exit 1; }
[ -d "$APP/.git" ] || { echo "!! $APP isn't a git clone - run bootstrap.sh once to make it one" >&2; exit 1; }
cd "$APP"

before=$(git rev-parse HEAD)
echo "== git pull"
if ! git pull -q --ff-only; then
    echo "!! $APP has changes of its own that clash with GitHub - see: git status" >&2
    exit 1
fi
if [ "$(git rev-parse HEAD)" = "$before" ]; then
    echo "   already up to date: $(git log -1 --format='%h %s (%cd)' --date=short)"
    exit 0
fi
git log --format='   %h %s' "$before..HEAD"

if ! git diff --quiet "$before" HEAD -- pyproject.toml 'requirements*.txt'; then
    echo "== Python packages"
    "$APP/.venv/bin/pip" install -q -e "${APP}[web,hardware]"
fi

if systemctl --user is-active --quiet outpost.service || systemctl --user is-enabled --quiet outpost.service 2>/dev/null; then
    echo "== restarting Outpost"
    systemctl --user restart outpost.service
fi
echo "== done"
