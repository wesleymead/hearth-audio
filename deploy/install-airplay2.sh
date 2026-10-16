#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this installer with sudo." >&2
    exit 1
fi

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
BUILD_DIR=$(mktemp -d /tmp/hearth-airplay-build.XXXXXX)
trap 'rm -rf "$BUILD_DIR"' EXIT HUP INT TERM

# The hub runs as the unprivileged `hearth` account and must be able to enter
# its installed working directory.
chmod 0755 "$PROJECT_DIR"

# Raspberry Pi OS commonly starts apt-daily shortly after boot. Let apt wait
# for that legitimate package-manager owner instead of failing the deployment;
# never remove dpkg's lock file manually.
apt-get -o DPkg::Lock::Timeout=300 update
apt-get -o DPkg::Lock::Timeout=300 install -y --no-install-recommends \
    build-essential git autoconf automake libtool pkg-config \
    libpopt-dev libconfig-dev libasound2-dev avahi-daemon \
    libavahi-client-dev libssl-dev libsoxr-dev libplist-dev libdbus-1-dev libglib2.0-dev dbus \
    libsodium-dev uuid-dev libgcrypt-dev xxd libplist-utils \
    libavutil-dev libavcodec-dev libavformat-dev systemd-dev snapserver

git clone --depth 1 https://github.com/mikebrady/nqptp.git "$BUILD_DIR/nqptp"
cd "$BUILD_DIR/nqptp"
autoreconf -fi
./configure --with-systemd-startup
make -j2
make install

git clone --branch 5.2.1 --depth 1 https://github.com/mikebrady/shairport-sync.git "$BUILD_DIR/shairport-sync"
cd "$BUILD_DIR/shairport-sync"
autoreconf -fi
./configure --sysconfdir=/etc --with-alsa --with-pipe --with-soxr \
    --with-avahi --with-ssl=openssl --with-systemd-startup --with-airplay-2 \
    --with-metadata --with-metadata-pipe --with-dbus-interface
make -j2
make install

install -m 0644 -b -S .hearth-backup "$PROJECT_DIR/deploy/shairport-sync.conf" /etc/shairport-sync.conf
install -m 0644 -b -S .hearth-backup "$PROJECT_DIR/deploy/snapserver.conf" /etc/snapserver.conf
sh "$PROJECT_DIR/deploy/install-snapweb.sh"
install -m 0755 "$PROJECT_DIR/deploy/hearth-playback-mode" /usr/local/sbin/hearth-playback-mode
install -m 0440 "$PROJECT_DIR/deploy/hearth-playback-mode.sudoers" /etc/sudoers.d/hearth-playback-mode
install -m 0644 "$PROJECT_DIR/deploy/hearth-audio-tmpfiles.conf" /etc/tmpfiles.d/hearth-audio.conf
install -m 0644 "$PROJECT_DIR/deploy/hearth-hub.service" /etc/systemd/system/hearth-hub.service
install -m 0755 "$PROJECT_DIR/deploy/hearth-display-power" /usr/local/sbin/hearth-display-power
install -m 0440 "$PROJECT_DIR/deploy/hearth-display-power.sudoers" /etc/sudoers.d/hearth-display-power

for SERVICE_USER in shairport-sync _shairport-sync snapserver _snapserver hearth; do
    if id "$SERVICE_USER" >/dev/null 2>&1; then
        usermod -a -G audio "$SERVICE_USER"
    fi
done

systemd-tmpfiles --remove hearth-audio.conf || true
systemd-tmpfiles --create hearth-audio.conf
chown _snapserver:audio /run/hearth-audio /run/hearth-audio/airplay.pcm /run/hearth-audio/soundboard.pcm
chown hearth:audio /run/hearth-audio/metadata
chmod 0770 /run/hearth-audio
chmod 0660 /run/hearth-audio/airplay.pcm /run/hearth-audio/soundboard.pcm /run/hearth-audio/metadata
systemctl daemon-reload
systemctl reset-failed snapserver || true
systemctl enable --now nqptp snapserver shairport-sync hearth-hub
systemctl restart hearth-hub
/usr/local/sbin/hearth-playback-mode stereo
if systemctl list-unit-files snapclient.service --no-legend 2>/dev/null | grep -q '^snapclient.service'; then
    systemctl enable snapclient
    systemctl restart snapclient
fi
sleep 2

for SERVICE in nqptp snapserver shairport-sync hearth-hub; do
    if ! systemctl --quiet is-active "$SERVICE"; then
        systemctl --no-pager --full status "$SERVICE" || true
        exit 1
    fi
done
if systemctl list-unit-files snapclient.service --no-legend 2>/dev/null | grep -q '^snapclient.service' \
    && ! systemctl --quiet is-active snapclient; then
    systemctl --no-pager --full status snapclient || true
    exit 1
fi

echo "Hearth Audio is now advertised as an AirPlay 2 receiver."
echo "Check it with: systemctl status nqptp shairport-sync snapserver snapclient hearth-hub"
