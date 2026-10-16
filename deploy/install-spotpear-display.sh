#!/bin/sh
set -eu

CONFIG=${1:-/etc/hearth-audio/node.json}
OVERLAY_NAME=spotpear_240x240_st7789_lcd1inch54
OVERLAY_URL=https://cdn.static.spotpear.com/uploads/download/diver/gm154/spotpear_240x240_st7789_lcd1inch54.dtbo
OVERLAY_SHA256=1921351a6e55d30f10898e52a0e081fde2725af9b6177f06b05c6cca6fe8a366

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this display installer with sudo." >&2
    exit 1
fi

DISPLAY_TYPE=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("display", {}).get("type", "hdmi"))' "$CONFIG")
if [ "$DISPLAY_TYPE" != "spotpear_st7789" ]; then
    exit 0
fi

if [ -f /boot/firmware/config.txt ]; then
    BOOT_CONFIG=/boot/firmware/config.txt
    OVERLAY_DIR=/boot/firmware/overlays
else
    BOOT_CONFIG=/boot/config.txt
    OVERLAY_DIR=/boot/overlays
fi

TMP=$(mktemp -d /tmp/hearth-spotpear.XXXXXX)
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT HUP INT TERM

if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$OVERLAY_URL" -o "$TMP/$OVERLAY_NAME.dtbo"
else
    wget -qO "$TMP/$OVERLAY_NAME.dtbo" "$OVERLAY_URL"
fi
printf '%s  %s\n' "$OVERLAY_SHA256" "$TMP/$OVERLAY_NAME.dtbo" | sha256sum -c -
install -d -m 0755 "$OVERLAY_DIR"
install -m 0644 "$TMP/$OVERLAY_NAME.dtbo" "$OVERLAY_DIR/$OVERLAY_NAME.dtbo"

python3 - "$BOOT_CONFIG" "$OVERLAY_NAME" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
overlay = sys.argv[2]
start = "# BEGIN Hearth SpotPear 1.54 display"
end = "# END Hearth SpotPear 1.54 display"
block = "\n".join((
    start,
    "[all]",
    "dtparam=spi=on",
    f"dtoverlay={overlay}",
    end,
))
text = path.read_text(encoding="utf-8")
if start in text and end in text:
    before, remainder = text.split(start, 1)
    _, after = remainder.split(end, 1)
    text = before.rstrip() + "\n\n" + block + after
else:
    text = text.rstrip() + "\n\n" + block + "\n"
temporary = path.with_suffix(path.suffix + ".hearth-new")
temporary.write_text(text, encoding="utf-8")
temporary.chmod(path.stat().st_mode)
temporary.replace(path)
PY

echo "SpotPear ST7789V display configured; reboot is required before /dev/fb1 appears."
