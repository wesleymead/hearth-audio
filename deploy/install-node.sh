#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this installer with sudo." >&2
    exit 1
fi

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
NODE_CONFIG_NAME=${1:-auto}
case "$NODE_CONFIG_NAME" in
    auto) ;;
    *.json)
        case "$NODE_CONFIG_NAME" in
            *[!a-zA-Z0-9._-]*|.*|*..*) echo "Unsafe node configuration name." >&2; exit 2 ;;
        esac ;;
    *) echo "Unsupported node configuration: $NODE_CONFIG_NAME" >&2; exit 2 ;;
esac
if [ "$NODE_CONFIG_NAME" != auto ]; then
    NODE_CONFIG="$PROJECT_DIR/deploy/$NODE_CONFIG_NAME"
    [ -f "$NODE_CONFIG" ] || { echo "Node configuration not found: $NODE_CONFIG" >&2; exit 2; }
fi
DISPLAY_USER=${HEARTH_DISPLAY_USER:-${SUDO_USER:-}}
if [ -z "$DISPLAY_USER" ] || [ "$DISPLAY_USER" = root ]; then
    DISPLAY_USER=$(getent passwd | awk -F: '$3 == 1000 { print $1; exit }')
fi
if [ -z "$DISPLAY_USER" ] || ! id "$DISPLAY_USER" >/dev/null 2>&1; then
    echo "Set HEARTH_DISPLAY_USER to your Raspberry Pi OS login user." >&2
    exit 2
fi
chmod 0755 "$PROJECT_DIR"

apt-get -o DPkg::Lock::Timeout=300 update
apt-get -o DPkg::Lock::Timeout=300 install -y --no-install-recommends \
    python3 python3-gpiozero python3-pygame fonts-dejavu-core libegl1 libgles2 \
    snapclient alsa-utils v4l-utils edid-decode network-manager cec-utils curl

if ! id hearth >/dev/null 2>&1; then
    useradd --system --create-home --home-dir /var/lib/hearth-audio --shell /usr/sbin/nologin hearth
fi

for NODE_GROUP in audio video gpio; do
    if getent group "$NODE_GROUP" >/dev/null 2>&1; then
        usermod -a -G "$NODE_GROUP" hearth
    fi
done

install -d -m 0750 -o hearth -g hearth /etc/hearth-audio
if [ ! -f /etc/hearth-audio/node.json ]; then
    if [ "$NODE_CONFIG_NAME" = auto ]; then
        if [ -n "${HEARTH_BUTTON_GPIO:-}" ]; then
            python3 "$PROJECT_DIR/deploy/provision-node.py" --hub "${HEARTH_HUB_URL:-http://hearth-hub.local:8080}" --display "${HEARTH_DISPLAY:-hdmi}" --button-gpio "$HEARTH_BUTTON_GPIO"
        else
            python3 "$PROJECT_DIR/deploy/provision-node.py" --hub "${HEARTH_HUB_URL:-http://hearth-hub.local:8080}" --display "${HEARTH_DISPLAY:-hdmi}"
        fi
    else
        install -m 0640 -o hearth -g hearth "$NODE_CONFIG" /etc/hearth-audio/node.json
    fi
fi
chown hearth:hearth /etc/hearth-audio/node.json
chmod 0640 /etc/hearth-audio/node.json
python3 "$PROJECT_DIR/deploy/enable-node-audio-output.py" /etc/hearth-audio/node.json

usermod -a -G hearth "$DISPLAY_USER"

install -m 0644 "$PROJECT_DIR/deploy/hearth-node.service" /etc/systemd/system/hearth-node.service
install -m 0755 "$PROJECT_DIR/deploy/hearth-node-display" /usr/local/bin/hearth-node-display
install -m 0755 "$PROJECT_DIR/deploy/hearth-node-display-prepare" /usr/local/sbin/hearth-node-display-prepare
install -m 0755 "$PROJECT_DIR/deploy/hearth-node-hdmi-display" /usr/local/bin/hearth-node-hdmi-display
install -m 0755 "$PROJECT_DIR/deploy/hearth-node-hdmi-display-prepare" /usr/local/sbin/hearth-node-hdmi-display-prepare
python3 "$PROJECT_DIR/deploy/render-node-units.py" --user "$DISPLAY_USER" --source "$PROJECT_DIR/deploy"
install -m 0644 -b -S .hearth-backup "$PROJECT_DIR/deploy/snapclient-node.conf" /etc/default/snapclient
python3 "$PROJECT_DIR/deploy/render-node-snapclient.py"
install -m 0755 "$PROJECT_DIR/deploy/hearth-snapclient-output" /usr/local/sbin/hearth-snapclient-output
install -m 0440 "$PROJECT_DIR/deploy/hearth-snapclient-output.sudoers" /etc/sudoers.d/hearth-snapclient-output
install -m 0755 "$PROJECT_DIR/deploy/hearth-snapclient-gain" /usr/local/sbin/hearth-snapclient-gain
install -m 0440 "$PROJECT_DIR/deploy/hearth-snapclient-gain.sudoers" /etc/sudoers.d/hearth-snapclient-gain
install -m 0755 "$PROJECT_DIR/deploy/hearth-node-power" /usr/local/sbin/hearth-node-power
install -m 0440 "$PROJECT_DIR/deploy/hearth-node-power.sudoers" /etc/sudoers.d/hearth-node-power

sh "$PROJECT_DIR/deploy/install-spotpear-display.sh" /etc/hearth-audio/node.json

OUTPUT_PROFILE=$(python3 -c 'import json,sys; c=json.load(open(sys.argv[1], encoding="utf-8")); n=int(c.get("streamChannels", 2)); p=c.get("audioProfile", "auto"); print("hdmi_51_pcm" if n == 6 else "hdmi_71_pcm" if n == 8 else p if p in {"auto", "analog_stereo", "hdmi_stereo"} else "auto")' /etc/hearth-audio/node.json)
if ! /usr/local/sbin/hearth-snapclient-output "$OUTPUT_PROFILE"; then
    if [ "$OUTPUT_PROFILE" != "auto" ] && /usr/local/sbin/hearth-snapclient-output auto; then
        echo "Saved output '$OUTPUT_PROFILE' is currently unavailable; using automatic fallback until it is reselected."
    else
        echo "No physical audio output is currently available; using the silent standby sink."
        /usr/local/sbin/hearth-snapclient-output null
    fi
fi
OUTPUT_GAIN=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("outputGainDb", 9))' /etc/hearth-audio/node.json)
/usr/local/sbin/hearth-snapclient-gain "$OUTPUT_GAIN"

# Raspberry Pi OS Bookworm and newer manage desktop output selection through
# PipeWire. Do not invoke raspi-config's legacy audio-output selector here: it
# opens an interactive warning when PipeWire or PulseAudio is active. Snapclient
# uses the system's current default output; the user can change that from the
# Raspberry Pi OS audio taskbar control without reinstalling this service.

DISPLAY_TYPE=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("display", {}).get("type", "hdmi"))' /etc/hearth-audio/node.json)
systemctl daemon-reload
systemctl enable --now snapclient hearth-node
if [ "$DISPLAY_TYPE" != none ]; then
    systemctl enable --now hearth-node-display
else
    systemctl disable --now hearth-node-display 2>/dev/null || true
fi
if [ "$DISPLAY_TYPE" = "spotpear_st7789" ]; then
    systemctl enable --now hearth-node-hdmi-display
else
    systemctl disable --now hearth-node-hdmi-display 2>/dev/null || true
fi
systemctl restart snapclient hearth-node
if [ "$DISPLAY_TYPE" != none ]; then
    systemctl restart hearth-node-display
fi
if [ "$DISPLAY_TYPE" = "spotpear_st7789" ]; then
    systemctl restart hearth-node-hdmi-display
fi
sleep 2

NODE_SERVICES="snapclient hearth-node"
if [ "$DISPLAY_TYPE" != none ]; then
    NODE_SERVICES="$NODE_SERVICES hearth-node-display"
fi
if [ "$DISPLAY_TYPE" = "spotpear_st7789" ]; then
    NODE_SERVICES="$NODE_SERVICES hearth-node-hdmi-display"
fi
for NODE_SERVICE in $NODE_SERVICES; do
    if ! systemctl --quiet is-active "$NODE_SERVICE"; then
        systemctl --no-pager --full status "$NODE_SERVICE" || true
        journalctl -u "$NODE_SERVICE" -n 50 --no-pager || true
        exit 1
    fi
done

NODE_NAME=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["name"])' /etc/hearth-audio/node.json)
echo "$NODE_NAME is installed. Open Add node on the hub, then restart hearth-node while the pairing window is open."
echo "If a physical pairing button was configured, you can hold it for five seconds instead."
