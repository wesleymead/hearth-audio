#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this installer with sudo." >&2
    exit 1
fi
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if [ ! -f "$PROJECT_DIR/hub/server.py" ]; then
    echo "Hearth source files are missing." >&2
    exit 2
fi
apt-get -o DPkg::Lock::Timeout=300 update
apt-get -o DPkg::Lock::Timeout=300 install -y --no-install-recommends python3 ffmpeg curl unzip
if ! id hearth >/dev/null 2>&1; then
    useradd --system --create-home --home-dir /var/lib/hearth-audio --shell /usr/sbin/nologin hearth
fi
install -d -m 0755 -o hearth -g hearth /var/lib/hearth-audio
sh "$PROJECT_DIR/deploy/install-airplay2.sh"
sh "$PROJECT_DIR/deploy/install-hub-audio.sh"
echo "Hub installed. Open http://$(hostname).local:8080 on your local network."
