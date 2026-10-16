#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this installer with sudo." >&2
    exit 1
fi

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

apt-get update
apt-get install -y --no-install-recommends snapclient alsa-utils

if id _snapclient >/dev/null 2>&1 && getent group audio >/dev/null 2>&1; then
    usermod -a -G audio _snapclient
fi

install -m 0644 -b -S .hearth-backup "$PROJECT_DIR/deploy/snapclient-hub.conf" /etc/default/snapclient
install -m 0755 "$PROJECT_DIR/deploy/hearth-snapclient-output" /usr/local/sbin/hearth-snapclient-output
install -m 0440 "$PROJECT_DIR/deploy/hearth-snapclient-output.sudoers" /etc/sudoers.d/hearth-snapclient-output
install -m 0755 "$PROJECT_DIR/deploy/hearth-snapclient-gain" /usr/local/sbin/hearth-snapclient-gain
install -m 0440 "$PROJECT_DIR/deploy/hearth-snapclient-gain.sudoers" /etc/sudoers.d/hearth-snapclient-gain
systemctl daemon-reload
systemctl enable snapclient
# Resolve and probe a real ALSA device instead of leaving the system service on
# a desktop-session `default` device that may not exist for `_snapclient`.
/usr/local/sbin/hearth-snapclient-output auto
sleep 2

if ! systemctl --quiet is-active snapclient; then
    systemctl --no-pager --full status snapclient || true
    exit 1
fi

echo "The Hearth hub is now a synchronized Snapcast audio location."
