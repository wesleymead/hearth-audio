#!/bin/sh
set -eu

VERSION="0.9.3"
SHA256="cc258df98b8c474ea19048bc84c0c2e2c5fbc3b0856aef03a838f3a4489fa4c7"
URL="https://github.com/snapcast/snapweb/releases/download/v${VERSION}/snapweb.zip"
DESTINATION="/usr/share/snapserver/snapweb"
MARKER="$DESTINATION/.hearth-version"

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this installer with sudo." >&2
    exit 1
fi

if [ -r "$MARKER" ] \
    && [ "$(cat "$MARKER")" = "$VERSION" ] \
    && [ -r "$DESTINATION/index.html" ] \
    && ! grep -q "Snapweb Placeholder" "$DESTINATION/index.html"; then
    exit 0
fi

STAGE=$(mktemp -d /tmp/hearth-snapweb.XXXXXX)
cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT HUP INT TERM
ARCHIVE="$STAGE/snapweb.zip"

if command -v curl >/dev/null 2>&1; then
    curl -fL --retry 3 --connect-timeout 15 -o "$ARCHIVE" "$URL"
elif command -v wget >/dev/null 2>&1; then
    wget -O "$ARCHIVE" "$URL"
else
    echo "Install curl or wget before installing Snapweb." >&2
    exit 1
fi

printf '%s  %s\n' "$SHA256" "$ARCHIVE" | sha256sum -c -
mkdir "$STAGE/site"
if command -v unzip >/dev/null 2>&1; then
    unzip -q "$ARCHIVE" -d "$STAGE/site"
else
    python3 -m zipfile -e "$ARCHIVE" "$STAGE/site"
fi

if [ ! -r "$STAGE/site/index.html" ] || [ ! -d "$STAGE/site/assets" ]; then
    echo "The downloaded Snapweb archive is incomplete." >&2
    exit 1
fi

install -d -m 0755 "$DESTINATION"
cp -a "$STAGE/site/." "$DESTINATION/"
chmod -R a+rX "$DESTINATION"
printf '%s\n' "$VERSION" > "$MARKER"
