"""Prototype Raspberry Pi node agent.

Read-only capability discovery works on an ordinary Raspberry Pi. Audio, IR,
and Wi-Fi mutations are disabled unless ``allowSystemChanges`` is explicitly
enabled in the node configuration.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import queue
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


DISCOVERY_PORT = 45820
DISCOVERY_REQUEST = b"HEARTH_NODE_DISCOVER_V1"
OUTPUT_GAINS = {-6, -3, 0, 3, 6, 9, 12}


def run(command: list[str], timeout: float = 4) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)


def hardware_model() -> str:
    model_path = Path("/proc/device-tree/model")
    if model_path.exists():
        return model_path.read_bytes().rstrip(b"\x00").decode("utf-8", errors="replace")
    return "Development computer"


def network_status() -> tuple[dict[str, Any], dict[str, Any]]:
    connection = {"type": "ethernet", "address": "127.0.0.1", "quality": 100}
    wifi = {"supported": False, "status": "unavailable", "ssid": "", "signal": 0}

    interfaces = Path("/sys/class/net")
    wifi_interfaces = list(interfaces.glob("wlan*")) if interfaces.exists() else []
    wifi["supported"] = bool(wifi_interfaces)
    if wifi_interfaces and shutil.which("nmcli"):
        try:
            result = run(["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "device", "wifi"])
            for line in result.stdout.splitlines():
                if line.startswith("yes:"):
                    _, ssid, signal = (line.split(":", 2) + ["", ""])[:3]
                    wifi.update({"status": "connected", "ssid": ssid, "signal": int(signal or 0)})
                    connection.update({"type": "wifi", "quality": wifi["signal"]})
                    break
        except subprocess.TimeoutExpired:
            # A busy NetworkManager must not kill the long-lived Hub
            # heartbeat thread. Keep the last safe, conservative status.
            wifi["status"] = "probe timed out"

    if shutil.which("hostname"):
        result = run(["hostname", "-I"])
        addresses = result.stdout.split()
        if addresses:
            connection["address"] = addresses[0]
    return connection, wifi


def snapcast_client_id() -> str:
    interfaces = Path("/sys/class/net")
    if not interfaces.exists():
        return ""
    preferred = sorted(interfaces.glob("eth*")) + sorted(interfaces.glob("wlan*"))
    for interface in preferred:
        try:
            address = (interface / "address").read_text(encoding="utf-8").strip().lower()
        except OSError:
            continue
        if address and address != "00:00:00:00:00:00":
            return address
    return ""


def snapclient_soundcard() -> str:
    config = Path("/etc/default/snapclient")
    try:
        content = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "default"
    match = re.search(r"(?:--soundcard(?:=|\s+)|-s\s+)([^\s\"]+)", content)
    return match.group(1) if match else "default"


def _edid_decode() -> tuple[bool, str, int, list[str]]:
    edid_files = list(Path("/sys/class/drm").glob("card*-HDMI-A-*/edid"))
    for edid_path in edid_files:
        try:
            payload = edid_path.read_bytes()
        except OSError:
            continue
        if not payload or not any(payload):
            continue
        sink = edid_path.parent.name
        formats = ["PCM"]
        max_channels = 2
        if shutil.which("edid-decode"):
            result = subprocess.run(
                ["edid-decode"], input=payload, capture_output=True, timeout=4, check=False
            )
            text = result.stdout.decode("utf-8", errors="replace")
            name_match = re.search(r"Display Product Name: ['\"]?([^'\"\n]+)", text)
            if name_match:
                sink = name_match.group(1).strip()
            channel_matches = [int(value) for value in re.findall(r"(?:Max channels|Channels):\s*(\d+)", text, re.I)]
            if channel_matches:
                max_channels = max(channel_matches)
            format_map = {
                "AC-3": "Dolby Digital",
                "Enhanced AC-3": "Dolby Digital Plus",
                "DTS-HD": "DTS-HD",
                "DTS:X": "DTS:X",
                "DTS": "DTS",
                "MAT": "Dolby MAT",
                "MLP": "Dolby TrueHD",
            }
            for marker, label in format_map.items():
                if marker.lower() in text.lower() and label not in formats:
                    formats.append(label)
        return True, sink, max_channels, formats
    return False, "No HDMI receiver detected", 2, ["PCM"]


def audio_capabilities(model: str, stream_channels: int = 2) -> dict[str, Any]:
    if stream_channels not in {2, 6, 8}:
        stream_channels = 2
    hdmi_connected, sink, channels, formats = _edid_decode()
    pi2 = "Raspberry Pi 2 Model B" in model
    analog = pi2 or "Raspberry Pi 3" in model or "Raspberry Pi 4" in model or "Development" in model
    profiles = [
        {"id": "auto", "label": "Auto · connected synchronized output", "available": analog or hdmi_connected, "detail": "Uses an output compatible with the Hub's current synchronized stream."},
        {"id": "no_output", "label": "No Output", "available": False, "detail": "The node is online, but no physical audio output is currently connected."},
        {"id": "analog_stereo", "label": "3.5 mm · Stereo", "available": analog and stream_channels == 2, "detail": "Two-channel output. Raspberry Pi hardware cannot detect whether a 3.5 mm cable is physically inserted." if stream_channels == 2 else "Analog output is unavailable while the Hub is sending multichannel PCM."},
        {"id": "analog_mono", "label": "3.5 mm · Mono mix", "available": False, "detail": "Unavailable until a verified ALSA downmix route is installed."},
        {"id": "hdmi_stereo", "label": "HDMI · Stereo PCM", "available": hdmi_connected and stream_channels == 2, "detail": "Two-channel PCM; selectable only while HDMI hot-plug and EDID are detected." if stream_channels == 2 else "Select Stereo as the Hub playback mode to use this output."},
        {"id": "hdmi_51_pcm", "label": "HDMI · 5.1 PCM", "available": hdmi_connected and stream_channels == 6 and channels >= 6, "detail": "Discrete 5.1 PCM output synchronized by Snapcast." if stream_channels == 6 and channels >= 6 else "Requires the Hub's 5.1 mode and an HDMI sink advertising at least six PCM channels."},
        {"id": "hdmi_71_pcm", "label": "HDMI · 7.1 PCM", "available": hdmi_connected and stream_channels == 8 and channels >= 8, "detail": "Discrete 7.1 PCM output synchronized by Snapcast." if stream_channels == 8 and channels >= 8 else "Requires the Hub's 7.1 mode and an HDMI sink advertising eight PCM channels."},
        {
            "id": "hdmi_atmos_direct",
            "label": "HDMI · Atmos direct passthrough",
            "available": False,
            "detail": "Unavailable in synchronized playback. A separate direct-passthrough player and supported Pi 4/5 hardware are required.",
        },
        {
            "id": "hdmi_dts_direct",
            "label": "HDMI · DTS direct passthrough",
            "available": False,
            "detail": "DTS is an encoded bitstream. The synchronized AirPlay/Snapcast path carries decoded PCM, so a separate direct player is required.",
        },
        {
            "id": "hdmi_dts_hd_direct",
            "label": "HDMI · DTS-HD / DTS:X direct passthrough",
            "available": False,
            "detail": "Requires original encoded media, a passthrough-capable direct player, compatible Pi hardware, and an AVR that advertises the format.",
        },
        {
            "id": "avr_dts_neo",
            "label": "AVR · DTS Neo upmix",
            "available": False,
            "detail": "DTS Neo is receiver-side stereo upmixing, not an HDMI stream format. Keep HDMI Stereo selected and activate Neo with an imported AVR IR action.",
        },
    ]
    active_profile = (
        "hdmi_51_pcm" if stream_channels == 6
        else "hdmi_71_pcm" if stream_channels == 8
        else "hdmi_stereo" if hdmi_connected
        else "analog_stereo" if analog
        else "no_output"
    )
    return {
        "requestedProfile": "auto",
        "activeProfile": active_profile,
        "outputGainDb": 9,
        "profiles": profiles,
        "detected": {
            "analog": analog,
            "hdmiConnected": hdmi_connected,
            "sink": sink,
            "maxPcmChannels": channels,
            "formats": formats,
            "streamChannels": stream_channels,
            "surroundSynchronized": stream_channels in {6, 8},
        },
    }


def classify_button_press(duration: float, long_press: float, reset_press: float) -> str:
    """Classify a released button without triggering destructive behavior."""

    if long_press <= 0 or reset_press <= long_press:
        raise ValueError("Button thresholds must satisfy 0 < long < reset.")
    if duration >= reset_press:
        return "very_long"
    if duration >= long_press:
        return "long"
    return "short"


class NodeRuntime:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.model = hardware_model()
        stream_channels = int(config.get("streamChannels", 2))
        self.audio = audio_capabilities(self.model, stream_channels)
        self.audio["detected"]["activeDevice"] = snapclient_soundcard()
        if self.audio["detected"]["activeDevice"] == "null":
            self.audio["activeProfile"] = "no_output"
        gain = int(config.get("outputGainDb", 9))
        self.audio["outputGainDb"] = gain if gain in OUTPUT_GAINS else 9
        requested_profile = str(config.get("audioProfile", "auto"))
        available_profiles = {item["id"] for item in self.audio["profiles"] if item["available"]}
        self.audio["requestedProfile"] = requested_profile if requested_profile in available_profiles else "auto"
        if self.audio["requestedProfile"] != "auto":
            self.audio["activeProfile"] = self.audio["requestedProfile"]
        self.volume = 50
        self.muted = False
        self.system_power_state = "awake"
        self.last_action: dict[str, Any] | None = None
        button_config = config.get("button", {})
        self.button_status = {
            "enabled": bool(button_config.get("enabled", False)),
            "gpio": int(button_config.get("gpio", 27)),
            "physicalPin": int(button_config.get("physicalPin", 13)),
            "groundPin": int(button_config.get("groundPin", 14)),
            "pullUp": bool(button_config.get("pullUp", True)),
            "status": "not_started" if button_config.get("enabled", False) else "disabled",
            "lastEvent": None,
        }
        self._last_stream_mode_attempt = 0.0
        self._gpio_button: Any | None = None
        self._button_pressed_at: float | None = None
        self.jobs: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=16)
        self.worker = threading.Thread(target=self._worker, daemon=True, name="node-actions")
        self.worker.start()

    @property
    def changes_allowed(self) -> bool:
        return bool(self.config.get("allowSystemChanges"))

    @property
    def audio_output_changes_allowed(self) -> bool:
        # Audio switching uses a separate, exact-command sudoers rule. Keep it
        # independent from broader Wi-Fi/IR operating-system changes so tests
        # and development runs remain non-mutating by default.
        return bool(self.config.get("allowAudioOutputChanges", self.changes_allowed))

    @property
    def node_power_changes_allowed(self) -> bool:
        return bool(self.config.get("allowNodePowerControl", False))

    def status(self) -> dict[str, Any]:
        connection, wifi = network_status()
        agent_url = str(self.config.get("advertisedUrl", "")).rstrip("/")
        if not agent_url:
            agent_url = f"http://{connection['address']}:{int(self.config.get('listenPort', 8090))}"
        return {
            "id": self.config["id"],
            "name": self.config.get("name", self.config["id"]),
            "room": self.config.get("room", "Unassigned"),
            "hardware": self.model,
            "snapcastClientId": snapcast_client_id(),
            "online": True,
            "enabled": True,
            "volume": self.volume,
            "muted": self.muted,
            "connection": connection,
            "wifi": wifi,
            "audio": self.audio,
            "power": {
                "controlType": "button",
                "stateMode": "stateless",
                "state": "unknown",
                "transport": "auto",
                "piPowerMode": "always_on",
                "shutdownDelaySeconds": 10,
                "onActionId": "",
                "offActionId": "",
                "pressActionId": "",
            },
            "systemPowerState": self.system_power_state,
            "agentUrl": agent_url,
            "button": self.button_status,
            "lastAction": self.last_action,
        }

    def start_button(self) -> None:
        settings = self.config.get("button", {})
        if not settings.get("enabled", False):
            return
        try:
            from gpiozero import Button

            self._gpio_button = Button(
                int(settings.get("gpio", 27)),
                pull_up=bool(settings.get("pullUp", True)),
                bounce_time=float(settings.get("bounceSeconds", 0.05)),
            )
            self._gpio_button.when_pressed = self._button_pressed
            self._gpio_button.when_released = self._button_released
            self.button_status["status"] = "ready"
        except Exception as exc:
            self.button_status.update({"status": "unavailable", "error": str(exc)})
            print(f"GPIO button unavailable: {exc}")

    def close_button(self) -> None:
        if self._gpio_button is not None:
            self._gpio_button.close()
            self._gpio_button = None

    def _button_pressed(self) -> None:
        self._button_pressed_at = time.monotonic()
        self.button_status["status"] = "pressed"

    def _button_released(self) -> None:
        if self._button_pressed_at is None:
            return
        duration = max(0.0, time.monotonic() - self._button_pressed_at)
        self._button_pressed_at = None
        settings = self.config.get("button", {})
        event = classify_button_press(
            duration,
            float(settings.get("longPressSeconds", 5.0)),
            float(settings.get("resetPressSeconds", 15.0)),
        )
        self.button_status["status"] = "ready"
        self.button_status["lastEvent"] = {
            "type": event,
            "durationSeconds": round(duration, 3),
            "at": datetime.now(timezone.utc).isoformat(),
        }
        threading.Thread(
            target=self._handle_button_event,
            args=(event, duration),
            daemon=True,
            name="button-event",
        ).start()

    def _handle_button_event(self, event: str, duration: float) -> None:
        if event == "long":
            pair_with_hub(self)
        self._send_button_event(event, duration)

    def _send_button_event(self, event: str, duration: float) -> None:
        hub_url = str(self.config.get("hubUrl", "")).rstrip("/")
        if not hub_url:
            return
        request = urllib.request.Request(
            f"{hub_url}/api/nodes/{self.config['id']}/button",
            data=json.dumps({"event": event, "durationSeconds": round(duration, 3)}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Hearth-Token": str(self.config.get("pairingToken", "")),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                result = json.loads(response.read() or b"{}")
                print(f"Button {event}: {result.get('message', 'accepted')}")
        except (urllib.error.URLError, TimeoutError) as exc:
            self.button_status["lastEvent"]["deliveryError"] = str(exc)
            print(f"Button event could not reach hub: {exc}")

    def rescan_audio(self) -> dict[str, Any]:
        requested = self.audio.get("requestedProfile", self.config.get("audioProfile", "auto"))
        stream_channels = int(self.config.get("streamChannels", 2))
        self.audio = audio_capabilities(self.model, stream_channels)
        self.audio["detected"]["activeDevice"] = snapclient_soundcard()
        if self.audio["detected"]["activeDevice"] == "null":
            self.audio["activeProfile"] = "no_output"
        gain = int(self.config.get("outputGainDb", 9))
        self.audio["outputGainDb"] = gain if gain in OUTPUT_GAINS else 9
        available = {item["id"] for item in self.audio["profiles"] if item["available"]}
        self.audio["requestedProfile"] = requested if requested in available else "auto"
        if self.audio["requestedProfile"] != "auto":
            self.audio["activeProfile"] = self.audio["requestedProfile"]
        self.audio["lastScan"] = datetime.now(timezone.utc).isoformat()
        return self.audio

    def recover_hdmi_audio(self) -> None:
        """Leave the silent standby sink when an HDMI receiver is reattached."""
        if not self.audio_output_changes_allowed or snapclient_soundcard() != "null":
            return
        if not _edid_decode()[0]:
            return
        self.rescan_audio()
        profile = str(self.config.get("audioProfile", "auto"))
        if profile not in {"auto", "hdmi_stereo", "hdmi_51_pcm", "hdmi_71_pcm"}:
            profile = "auto"
        self.apply_audio_profile(profile)
        print(f"Restored HDMI audio after display reconnection: {self.audio['detected']['activeDevice']}")

    def apply_stream_channels(self, channels: int) -> dict[str, Any]:
        if channels not in {2, 6, 8}:
            raise ValueError("Synchronized stream channels must be 2, 6, or 8.")
        current_channels = int(self.audio.get("detected", {}).get("streamChannels", 2))
        if channels == current_channels:
            return self.audio

        previous_audio = copy.deepcopy(self.audio)
        previous_config = copy.deepcopy(self.config)
        try:
            if channels == 2:
                requested_profile = str(self.config.get("stereoAudioProfile", "auto"))
            else:
                if current_channels == 2:
                    self.config["stereoAudioProfile"] = str(self.audio.get("requestedProfile", "auto"))
                requested_profile = "hdmi_51_pcm" if channels == 6 else "hdmi_71_pcm"

            refreshed = audio_capabilities(self.model, channels)
            available = {item["id"] for item in refreshed["profiles"] if item["available"]}
            if requested_profile not in available:
                target = "5.1" if channels == 6 else "7.1" if channels == 8 else "stereo"
                raise RuntimeError(f"The connected output does not support the Hub's {target} PCM mode.")

            self.audio = refreshed
            self.apply_audio_profile(requested_profile)
            self.config["streamChannels"] = channels
            if channels == 2:
                self.config.pop("stereoAudioProfile", None)
            save_node_config(self)
            return self.audio
        except Exception:
            self.audio = previous_audio
            self.config.clear()
            self.config.update(previous_config)
            raise

    def apply_audio_profile(self, profile: str) -> dict[str, Any]:
        available = {item["id"] for item in self.audio["profiles"] if item["available"]}
        if profile not in available:
            raise ValueError(f"Audio profile '{profile}' is unavailable on this node.")
        if profile == "auto":
            active_profile = next(
                (
                    candidate
                    for candidate in (
                        "hdmi_71_pcm", "hdmi_51_pcm", "hdmi_stereo", "analog_stereo", "i2s_stereo"
                    )
                    if candidate in available
                ),
                "analog_stereo",
            )
        else:
            active_profile = profile
        helper = Path("/usr/local/sbin/hearth-snapclient-output")
        # An installed helper may be present on a machine running tests, so
        # existence alone must never authorize a real ALSA reconfiguration.
        if self.audio_output_changes_allowed and helper.exists():
            result = run(["sudo", "-n", str(helper), active_profile], timeout=30)
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Audio output switch failed.")
            selected_device = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else snapclient_soundcard()
            self.audio["detected"]["activeDevice"] = selected_device
            self.apply_output_gain(int(self.config.get("outputGainDb", 9)), save=False)
        self.audio["requestedProfile"] = profile
        self.audio["activeProfile"] = active_profile
        self.config["audioProfile"] = profile
        save_node_config(self)
        return self.audio

    def apply_output_gain(self, gain: int, save: bool = True) -> dict[str, Any]:
        if gain not in OUTPUT_GAINS:
            raise ValueError("Output gain must be -6, -3, 0, +3, +6, +9, or +12 dB.")
        helper = Path("/usr/local/sbin/hearth-snapclient-gain")
        if self.audio_output_changes_allowed and helper.exists():
            result = run(["sudo", "-n", str(helper), str(gain)], timeout=8)
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Output gain change failed.")
        self.audio["outputGainDb"] = gain
        self.config["outputGainDb"] = gain
        if save:
            save_node_config(self)
        return self.audio

    def cec_power(self, state: str) -> dict[str, Any]:
        if state not in {"on", "off"}:
            raise ValueError("HDMI-CEC power state must be on or off.")
        if not self.config.get("allowCecControl", True):
            raise RuntimeError("HDMI-CEC control is disabled in the node configuration.")
        if not self.audio.get("detected", {}).get("hdmiConnected"):
            raise RuntimeError("No HDMI receiver is currently detected.")
        client = shutil.which("cec-client")
        if not client:
            raise RuntimeError("cec-client is not installed; install the cec-utils package.")
        target = min(max(int(self.config.get("cecLogicalAddress", 5)), 0), 15)
        command = f"on {target}" if state == "on" else f"standby {target}"
        result = subprocess.run(
            [client, "-s", "-d", "1"],
            input=command + "\n",
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "HDMI-CEC power command failed.")
        return {"accepted": True, "state": state, "logicalAddress": target}

    def system_power(self, state: str, delay_seconds: int = 0) -> dict[str, Any]:
        if state not in {"sleep", "wake", "halt"}:
            raise ValueError("Node power state must be sleep, wake, or halt.")
        if not self.node_power_changes_allowed:
            raise RuntimeError("Node power control is disabled in this node's configuration.")
        helper = Path("/usr/local/sbin/hearth-node-power")
        if not helper.exists():
            raise RuntimeError("The installed node power helper is missing.")
        delay_seconds = min(max(int(delay_seconds), 0), 60)

        def apply() -> None:
            if delay_seconds:
                time.sleep(delay_seconds)
            result = run(["sudo", "-n", str(helper), state], timeout=8)
            if result.returncode:
                print(result.stderr.strip() or result.stdout.strip() or f"Node {state} command failed")

        if state == "wake":
            apply()
            self.system_power_state = "awake"
        else:
            self.system_power_state = "sleeping" if state == "sleep" else "halting"
            threading.Thread(target=apply, daemon=True, name=f"node-power-{state}").start()
        return {"accepted": True, "state": self.system_power_state, "delaySeconds": delay_seconds}

    def queue_action(self, action: dict[str, Any], signals: list[dict[str, Any]]) -> None:
        self.jobs.put_nowait({"action": action, "signals": signals})

    def _worker(self) -> None:
        while True:
            job = self.jobs.get()
            action = job["action"]
            self.last_action = {"id": action.get("id"), "status": "running", "at": datetime.now(timezone.utc).isoformat()}
            try:
                for step in action.get("steps", []):
                    self._execute_step(step, job["signals"])
                self.last_action["status"] = "complete"
            except Exception as exc:  # Action failures are reported in status.
                self.last_action.update({"status": "failed", "error": str(exc)})
            finally:
                self.jobs.task_done()

    def _execute_step(self, step: dict[str, Any], signals: list[dict[str, Any]]) -> None:
        kind = step.get("type")
        if kind == "delay":
            time.sleep(min(max(int(step.get("milliseconds", 0)), 0), 30_000) / 1000)
        elif kind == "ir.send":
            signal = next((item for item in signals if item.get("name") == step.get("signal")), None)
            if not signal:
                raise ValueError(f"IR signal '{step.get('signal')}' was not found.")
            for _ in range(min(max(int(step.get("repeats", 1)), 1), 20)):
                self._send_ir(signal)
        elif kind == "node.mute":
            self.muted = bool(step.get("value"))
            self._alsa_mute(self.muted)
        elif kind == "node.volume":
            self.volume = min(max(int(step.get("value", 50)), 0), 100)
            self._alsa_volume(self.volume)
        elif kind == "audio.profile":
            profile = str(step.get("value", "auto"))
            available = {item["id"] for item in self.audio["profiles"] if item["available"]}
            if profile not in available:
                raise ValueError(f"Audio profile '{profile}' is unavailable.")
            self.audio["requestedProfile"] = profile

    def _send_ir(self, signal: dict[str, Any]) -> None:
        if not self.changes_allowed:
            return
        ir_ctl = shutil.which("ir-ctl")
        if not ir_ctl:
            raise RuntimeError("ir-ctl is not installed.")
        device = str(self.config.get("irDevice", "/dev/lirc0"))
        if signal.get("kind") == "parsed":
            protocol, scancode = self._parsed_scancode(signal)
            result = run([ir_ctl, "--device", device, "--scancode", f"{protocol}:0x{scancode:x}"], timeout=8)
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or "Parsed IR transmission failed.")
            return
        durations = signal.get("data", [])
        lines = [f"carrier {int(signal.get('frequency', 38000))}"]
        lines.extend(f"{'pulse' if index % 2 == 0 else 'space'} {duration}" for index, duration in enumerate(durations))
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".irctl", delete=False) as output:
            output.write("\n".join(lines) + "\n")
            temporary = output.name
        try:
            duty = round(float(signal.get("duty_cycle", 0.33)) * 100)
            result = run([ir_ctl, "--device", device, "--duty-cycle", str(duty), "--send", temporary], timeout=8)
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or "IR transmission failed.")
        finally:
            Path(temporary).unlink(missing_ok=True)

    @staticmethod
    def _parsed_scancode(signal: dict[str, Any]) -> tuple[str, int]:
        """Translate common Flipper parsed records to Linux ``ir-ctl`` scancodes."""

        protocol_name = str(signal.get("protocol", ""))
        address = int.from_bytes(bytes.fromhex(str(signal.get("address", ""))), "little")
        command = int.from_bytes(bytes.fromhex(str(signal.get("command", ""))), "little")
        if protocol_name == "NEC":
            return "nec", ((address & 0xFF) << 8) | (command & 0xFF)
        if protocol_name == "NECext":
            return "necx", ((address & 0xFFFF) << 8) | (command & 0xFF)
        if protocol_name == "RC5":
            return "rc5", ((address & 0x1F) << 8) | (command & 0x7F)
        if protocol_name == "RC5X":
            return "rc5x_20", ((address & 0x1F) << 8) | (command & 0xFF)
        if protocol_name == "RC6":
            return "rc6_0", ((address & 0xFF) << 8) | (command & 0xFF)
        sony_protocols = {"SIRC": ("sony12", 5), "SIRC15": ("sony15", 8), "SIRC20": ("sony20", 13)}
        if protocol_name in sony_protocols:
            protocol, address_bits = sony_protocols[protocol_name]
            return protocol, ((address & ((1 << address_bits) - 1)) << 7) | (command & 0x7F)
        raise RuntimeError(
            f"Flipper protocol {protocol_name!r} is not encoded by the prototype; use a raw capture."
        )

    def _alsa_mute(self, muted: bool) -> None:
        if self.changes_allowed and shutil.which("amixer"):
            run(["amixer", "sset", "PCM", "mute" if muted else "unmute"])

    def _alsa_volume(self, volume: int) -> None:
        if self.changes_allowed and shutil.which("amixer"):
            run(["amixer", "sset", "PCM", f"{volume}%"])

    def change_wifi(self, ssid: str, password: str) -> dict[str, Any]:
        if not self.changes_allowed:
            return {"accepted": True, "dryRun": True, "message": "Wi-Fi change validated; system changes are disabled."}
        if not shutil.which("nmcli"):
            raise RuntimeError("NetworkManager/nmcli is required to change Wi-Fi.")
        result = subprocess.run(
            ["nmcli", "--ask", "device", "wifi", "connect", ssid],
            input=password + "\n",
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Wi-Fi connection failed.")
        return {"accepted": True, "dryRun": False, "message": "NetworkManager accepted the new Wi-Fi connection."}


class NodeHandler(BaseHTTPRequestHandler):
    runtime: NodeRuntime
    server_version = "HearthNode/0.1"

    def log_message(self, message: str, *args: Any) -> None:
        print(f"{self.address_string()} - {message % args}")

    def _authorized(self) -> bool:
        expected = str(self.runtime.config.get("pairingToken", ""))
        return not expected or self.headers.get("X-Hearth-Token", "") == expected

    def _json(self, value: Any, status: int = HTTPStatus.OK) -> None:
        payload = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _body(self) -> dict[str, Any]:
        size = int(self.headers.get("Content-Length", "0"))
        if size > 1_500_000:
            raise ValueError("Request is too large.")
        value = json.loads(self.rfile.read(size) or b"{}")
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object.")
        return value

    def do_GET(self) -> None:  # noqa: N802
        if self.path in {"/api/v1/status", "/api/v1/capabilities"}:
            self._json(self.runtime.status())
        elif self.path == "/health":
            self._json({"ok": True})
        else:
            self._json({"error": "Not found."}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            self._json({"error": "Invalid pairing token."}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            body = self._body()
            if self.path == "/api/v1/actions/run":
                self.runtime.queue_action(body.get("action", {}), body.get("signals", []))
                self._json({"queued": True}, HTTPStatus.ACCEPTED)
            elif self.path == "/api/v1/audio/rescan":
                self._json({"audio": self.runtime.rescan_audio()})
            elif self.path == "/api/v1/audio/profile":
                self._json({"audio": self.runtime.apply_audio_profile(str(body.get("profile", "auto")))})
            elif self.path == "/api/v1/audio/gain":
                self._json({"audio": self.runtime.apply_output_gain(int(body.get("gainDb", 9)))})
            elif self.path == "/api/v1/cec/power":
                self._json(self.runtime.cec_power(str(body.get("state", ""))))
            elif self.path == "/api/v1/system/power":
                self._json(self.runtime.system_power(str(body.get("state", "")), int(body.get("delaySeconds", 0))))
            elif self.path == "/api/v1/wifi":
                ssid = str(body.get("ssid", "")).strip()
                if not ssid:
                    raise ValueError("Wi-Fi network name is required.")
                self._json(self.runtime.change_wifi(ssid, str(body.get("password", ""))), HTTPStatus.ACCEPTED)
            else:
                self._json({"error": "Not found."}, HTTPStatus.NOT_FOUND)
        except queue.Full:
            self._json({"error": "The node action queue is full."}, HTTPStatus.CONFLICT)
        except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)


def register_with_hub(runtime: NodeRuntime, announce: bool = True) -> bool:
    hub_url = str(runtime.config.get("hubUrl", "")).rstrip("/")
    if not hub_url:
        return False
    payload = runtime.status()
    payload["agentToken"] = runtime.config.get("pairingToken", "")
    request = urllib.request.Request(
        f"{hub_url}/api/nodes/register",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            registration = json.loads(response.read() or b"{}")
            if announce:
                print(f"Registered {runtime.config['id']} with {hub_url}")
            desired_channels = int(registration.get("playbackChannels", 2))
            current_channels = int(runtime.audio.get("detected", {}).get("streamChannels", 2))
            now = time.monotonic()
            if desired_channels != current_channels and now - runtime._last_stream_mode_attempt >= 30:
                runtime._last_stream_mode_attempt = now
                try:
                    runtime.apply_stream_channels(desired_channels)
                    if announce:
                        print(f"Configured {desired_channels}-channel synchronized playback")
                except (OSError, ValueError, RuntimeError) as error:
                    if announce:
                        print(f"Playback mode is not available on this node: {error}")
            return True
    except urllib.error.HTTPError as exc:
        if announce:
            print(f"Hub registration rejected ({exc.code}): {exc.reason}")
    except (urllib.error.URLError, TimeoutError) as exc:
        if announce:
            print(f"Hub registration deferred: {exc}")
    return False


def heartbeat_hub(runtime: NodeRuntime, interval: float = 5.0) -> None:
    while True:
        time.sleep(interval)
        try:
            runtime.recover_hdmi_audio()
            register_with_hub(runtime, announce=False)
        except Exception as error:
            # The agent should recover from a transient system probe failure;
            # a dead daemon thread otherwise makes a healthy Snapclient appear
            # offline forever until the service is restarted.
            print(f"Hub heartbeat deferred: {error}")


def discover_hub(timeout: float = 3.0) -> str:
    """Find a hub with an open pairing window using a local UDP broadcast."""

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        client.settimeout(timeout)
        client.bind(("0.0.0.0", 0))
        client.sendto(DISCOVERY_REQUEST, ("255.255.255.255", DISCOVERY_PORT))
        try:
            payload, address = client.recvfrom(2048)
        except TimeoutError:
            return ""
    try:
        response = json.loads(payload)
        if response.get("service") != "hearth-hub":
            return ""
        port = int(response.get("port", 8080))
    except (ValueError, TypeError, json.JSONDecodeError):
        return ""
    return f"http://{address[0]}:{port}"


def save_node_config(runtime: NodeRuntime) -> None:
    path_value = runtime.config.get("_configPath")
    if not path_value:
        return
    path = Path(str(path_value))
    content = {key: value for key, value in runtime.config.items() if not key.startswith("_")}
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        print(f"Node config could not be updated: {exc}")


def pair_with_hub(runtime: NodeRuntime) -> bool:
    if register_with_hub(runtime):
        return True
    discovered = discover_hub()
    if not discovered:
        print("No Hearth hub currently has an open pairing window.")
        return False
    runtime.config["hubUrl"] = discovered
    save_node_config(runtime)
    return register_with_hub(runtime)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Hearth Audio Raspberry Pi node.")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    config["_configPath"] = str(args.config.resolve())
    if not config.get("pairingToken") or str(config["pairingToken"]).startswith("replace-with"):
        config["pairingToken"] = secrets.token_urlsafe(32)
    runtime = NodeRuntime(config)
    runtime.start_button()
    save_node_config(runtime)
    pair_with_hub(runtime)
    threading.Thread(
        target=heartbeat_hub,
        args=(runtime,),
        daemon=True,
        name="hub-heartbeat",
    ).start()
    handler = type("ConfiguredNodeHandler", (NodeHandler,), {"runtime": runtime})
    server = ThreadingHTTPServer((config.get("listenHost", "0.0.0.0"), int(config.get("listenPort", 8090))), handler)
    print(f"Hearth node {config['id']} listening on port {config.get('listenPort', 8090)}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        runtime.close_button()
        server.server_close()


if __name__ == "__main__":
    main()
