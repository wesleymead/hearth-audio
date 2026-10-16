#!/usr/bin/env python3
"""Create a safe, unpaired node configuration without changing an existing one."""

from __future__ import annotations

import argparse
import json
import re
import socket
from pathlib import Path
from urllib.parse import urlparse


def pi_model(path: Path = Path("/proc/device-tree/model")) -> str:
    try:
        return path.read_bytes().decode("utf-8", "replace").rstrip("\0\n")
    except OSError:
        return "Unknown Raspberry Pi"


def make_config(hostname: str, hub_url: str, model: str, display: str = "hdmi", button_gpio: int | None = None) -> dict:
    node_id = re.sub(r"[^a-z0-9-]", "-", hostname.lower()).strip("-")
    if not node_id:
        raise ValueError("Set a unique hostname before installing this node")
    parsed = urlparse(hub_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Hub URL must be an http(s) URL without embedded credentials")
    if display not in {"hdmi", "none", "spotpear"}:
        raise ValueError("Unknown display type")
    if button_gpio is not None and not 2 <= button_gpio <= 27:
        raise ValueError("Button GPIO must be between 2 and 27")
    config = {
        "id": node_id,
        "name": hostname.replace("-", " ").title(),
        "room": "Unassigned",
        "hubUrl": hub_url.rstrip("/"),
        "listenHost": "0.0.0.0",
        "listenPort": 8090,
        "advertisedUrl": "",
        "pairingToken": "",
        "allowSystemChanges": False,
        "allowAudioOutputChanges": True,
        "allowNodePowerControl": True,
        "allowCecControl": True,
        "cecLogicalAddress": 5,
        "irDevice": "",
        "audioProfile": "auto",
        "outputGainDb": 0,
        "streamChannels": 2,
        "button": {
            "enabled": button_gpio is not None,
            "gpio": button_gpio or 27,
            "pullUp": True,
            "bounceSeconds": 0.05,
            "longPressSeconds": 5.0,
            "resetPressSeconds": 15.0,
        },
    }
    if display == "spotpear":
        config["display"] = {
            "type": "spotpear_st7789",
            "framebuffer": "/dev/fb1",
            "layout": "compact",
            "secondaryHdmi": True,
            "width": 240,
            "height": 240,
            "touchController": "xpt2046",
        }
    elif display == "none":
        config["display"] = {"type": "none"}
    # Audio capabilities and the connected HDMI receiver are probed at runtime.
    # Do not infer a GPIO button or a vendor HAT from the Pi model alone.
    return config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="/etc/hearth-audio/node.json")
    parser.add_argument("--hub", default="http://hearth-hub.local:8080")
    parser.add_argument("--display", choices=("hdmi", "none", "spotpear"), default="hdmi")
    parser.add_argument("--button-gpio", type=int)
    args = parser.parse_args()
    target = Path(args.output)
    if target.exists():
        print(f"Keeping existing paired configuration: {target}")
        return
    model = pi_model()
    if "Raspberry Pi" not in model:
        print(f"Warning: unrecognized model ({model}); check audio and display support.")
    config = make_config(socket.gethostname(), args.hub, model, args.display, args.button_gpio)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8") as output:
        json.dump(config, output, indent=2)
        output.write("\n")
    target.chmod(0o640)
    print(f"Created unpaired {model} configuration for {config['id']}")


if __name__ == "__main__":
    main()
