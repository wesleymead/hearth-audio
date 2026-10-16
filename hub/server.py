"""Dependency-free prototype hub API and static-file server."""

from __future__ import annotations

import argparse
import copy
import ipaddress
import json
import mimetypes
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import unquote
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from common.flipper_ir import InfraredFileError, parse_flipper_ir
from hub.metadata import ShairportMetadataReader
from hub.soundboard import MAX_UPLOAD, Soundboard
from node.agent import audio_capabilities, hardware_model


ROOT = Path(__file__).resolve().parent
STATIC_ROOT = ROOT / "static"
DEFAULT_STATE_PATH = ROOT / "default_state.json"
DISCOVERY_PORT = 45820
DISCOVERY_REQUEST = b"HEARTH_NODE_DISCOVER_V1"
DEMO_NODE_IDS = {"living-room-pi2", "kitchen-prototype"}
LOCAL_HUB_NODE_ID = "hearth-hub"
SNAPCAST_RPC_LOCK = threading.Lock()
NODE_HEARTBEAT_TIMEOUT = 15.0
DISPLAY_HEARTBEAT_TIMEOUT = 15.0
BROWSER_NODE_TIMEOUT = 30.0
PLAYBACK_MODES = {"stereo": 2, "surround_51": 6, "surround_71": 8}
OUTPUT_GAINS = {-6, -3, 0, 3, 6, 9, 12}
PLAYBACK_MODE_HELPER = "/usr/local/sbin/hearth-playback-mode"
DISPLAY_POWER_HELPER = "/usr/local/sbin/hearth-display-power"
OUTPUT_GAIN_HELPER = "/usr/local/sbin/hearth-snapclient-gain"


def local_hub_node() -> dict[str, Any]:
    model = hardware_model()
    audio = audio_capabilities(model)
    return {
        "id": LOCAL_HUB_NODE_ID,
        "name": "Hub",
        "room": "Hub",
        "hardware": f"{model} · Local playback",
        "local": True,
        "online": False,
        "enabled": True,
        "volume": 50,
        "muted": False,
        "connection": {"type": "local", "address": "127.0.0.1", "quality": 100},
        "button": {"enabled": False, "status": "not installed"},
        "wifi": {"supported": False, "status": "managed by hub", "ssid": "", "signal": 0},
        "audio": audio,
        "power": {
            "controlType": "switch",
            "stateMode": "assumed",
            "state": "on",
            "transport": "none",
            "piPowerMode": "always_on",
            "shutdownDelaySeconds": 10,
            "onActionId": "",
            "offActionId": "",
            "pressActionId": "",
        },
        "ir": {"fileName": "", "signals": []},
        "actions": [],
        "lastAction": None,
        "agentUrl": "",
    }


def snapcast_rpc(method: str, params: dict[str, Any] | None = None, timeout: float = 3.0) -> dict[str, Any]:
    request_id = int(time.time() * 1000000)
    request = {"id": request_id, "jsonrpc": "2.0", "method": method}
    if params is not None:
        request["params"] = params
    payload = json.dumps(request).encode("utf-8")
    http_request = urllib.request.Request(
        "http://127.0.0.1:1780/jsonrpc",
        data=payload,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    # One-shot HTTP RPC receives only this request's response, unlike a raw
    # control socket which can also receive asynchronous notifications.
    try:
        with SNAPCAST_RPC_LOCK:
            with urllib.request.urlopen(http_request, timeout=timeout) as http_response:
                response = json.loads(http_response.read())
    except (TimeoutError, socket.timeout) as exc:
        raise ValueError("Snapserver audio control timed out on HTTP port 1780.") from exc
    except urllib.error.HTTPError as exc:
        raise ValueError(f"Snapserver rejected audio control over HTTP: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"Snapserver audio control is unavailable on HTTP port 1780: {exc.reason}") from exc
    if "error" in response:
        raise ValueError(response["error"].get("message", "Snapserver rejected the request."))
    return response.get("result", {})


def snapclient_for_node(status: dict[str, Any], node: dict[str, Any]) -> dict[str, Any] | None:
    groups = status.get("server", {}).get("groups", [])
    clients = [client for group in groups for client in group.get("clients", [])]
    identifiers = {
        str(node.get("id", "")).lower().split("#", 1)[0],
        str(node.get("snapcastClientId", "")).lower().split("#", 1)[0],
    } - {""}
    if node.get("local"):
        # Snapserver may report the machine hostname separately from the
        # configured Snapclient host id. Include both for local playback.
        identifiers.add(socket.gethostname().lower())
    matches = [
        client
        for client in clients
        if str(client.get("id", "")).lower().split("#", 1)[0] in identifiers
        or str(client.get("host", {}).get("name", "")).lower() in identifiers
        or str(client.get("host", {}).get("mac", "")).lower() in identifiers
        or (
            node.get("local")
            and str(client.get("host", {}).get("ip", "")) in {"127.0.0.1", "::1"}
        )
    ]
    # Snapserver retains disconnected clients. After a reconnect, both the
    # stale and current Hub entries can match; always choose the live one.
    return next((client for client in matches if client.get("connected")), matches[0] if matches else None)


def local_snapclient(status: dict[str, Any]) -> dict[str, Any] | None:
    return snapclient_for_node(status, {"id": LOCAL_HUB_NODE_ID, "local": True})


class HubStore:
    def __init__(self, state_path: Path | None = None):
        self.state_path = state_path
        self.artwork_path = state_path.with_suffix(".artwork") if state_path else None
        source = state_path if state_path and state_path.exists() else DEFAULT_STATE_PATH
        self.state: dict[str, Any] = json.loads(source.read_text(encoding="utf-8"))
        self.lock = threading.RLock()
        # Browser playback locations are deliberately runtime-only. A closed
        # tab (or a Hub restart) must never leave a phantom room in state.json.
        self.browser_nodes: dict[str, dict[str, Any]] = {}
        self.artwork: bytes = b""
        self.artwork_type = "application/octet-stream"
        self.pairing_mode_until = 0.0
        self._transport_checked_at = 0.0
        self._transport_available = False
        self._local_audio_reset = False
        changed = self._normalise_state()
        if self.artwork_path and self.artwork_path.exists():
            try:
                self.artwork = self.artwork_path.read_bytes()
            except OSError:
                self.artwork = b""
            if self.artwork:
                self.artwork_type = (
                    "image/png" if self.artwork.startswith(b"\x89PNG\r\n\x1a\n")
                    else "image/jpeg" if self.artwork.startswith(b"\xff\xd8\xff")
                    else "application/octet-stream"
                )
        playback = self.state["system"]["playback"]
        if int(playback.get("artworkRevision", 0)) and not self.artwork:
            playback["artworkRevision"] = 0
            changed = True
        if changed:
            self.save()
        self.soundboard = Soundboard(state_path, lambda: self.state["nodes"] + list(self.browser_nodes.values()), snapcast_rpc, bool(self.state["system"]["settings"].get("soundboardEnabled", True)))

    def _normalise_state(self) -> bool:
        changed = False
        system = self.state.setdefault("system", {})
        schema_version = int(system.get("schemaVersion", 1))
        if schema_version < 2:
            original = self.state.get("nodes", [])
            self.state["nodes"] = [node for node in original if node.get("id") not in DEMO_NODE_IDS]
            system["schemaVersion"] = 2
            changed = True
        if not any(node.get("id") == LOCAL_HUB_NODE_ID for node in self.state.setdefault("nodes", [])):
            self.state["nodes"].insert(0, local_hub_node())
            changed = True
        # Older persisted states may contain the Hub room without the marker
        # used by the UI to suppress its power control.
        hub_node = next(node for node in self.state["nodes"] if node.get("id") == LOCAL_HUB_NODE_ID)
        if not hub_node.get("local"):
            hub_node["local"] = True
            changed = True
        actual_hardware = f"{hardware_model()} · Local playback"
        if hub_node.get("hardware") != actual_hardware:
            hub_node["hardware"] = actual_hardware
            requested = hub_node.get("audio", {}).get("requestedProfile", "auto")
            hub_node["audio"] = audio_capabilities(hardware_model())
            available = {profile["id"] for profile in hub_node["audio"]["profiles"] if profile["available"]}
            hub_node["audio"]["requestedProfile"] = requested if requested in available else "auto"
            changed = True
        if schema_version < 3:
            system["schemaVersion"] = 3
            changed = True
        if schema_version < 4:
            hub_node = next(node for node in self.state["nodes"] if node.get("id") == LOCAL_HUB_NODE_ID)
            requested = hub_node.get("audio", {}).get("requestedProfile", "auto")
            hub_node["audio"] = audio_capabilities(hardware_model())
            available = {profile["id"] for profile in hub_node["audio"]["profiles"] if profile["available"]}
            hub_node["audio"]["requestedProfile"] = requested if requested in available else "auto"
            if hub_node["audio"]["requestedProfile"] != "auto":
                hub_node["audio"]["activeProfile"] = hub_node["audio"]["requestedProfile"]
            system["schemaVersion"] = 4
            changed = True
        if schema_version < 5:
            # Refresh the local capability catalogue when new accurately
            # described direct/AVR formats are added to the interface.
            hub_node = next(node for node in self.state["nodes"] if node.get("id") == LOCAL_HUB_NODE_ID)
            requested = hub_node.get("audio", {}).get("requestedProfile", "auto")
            hub_node["audio"] = audio_capabilities(hardware_model())
            available = {profile["id"] for profile in hub_node["audio"]["profiles"] if profile["available"]}
            hub_node["audio"]["requestedProfile"] = requested if requested in available else "auto"
            if hub_node["audio"]["requestedProfile"] != "auto":
                hub_node["audio"]["activeProfile"] = hub_node["audio"]["requestedProfile"]
            system["schemaVersion"] = 5
            changed = True
        if schema_version < 6:
            # The Hub used to expose a power button. If it was pressed, its
            # persistent `enabled=false` state caused every future software
            # volume update to keep the local Snapclient muted. Hub playback
            # is now always enabled and is controlled only by mute/volume.
            hub_node = next(node for node in self.state["nodes"] if node.get("id") == LOCAL_HUB_NODE_ID)
            was_disabled = not bool(hub_node.get("enabled", True))
            hub_node["enabled"] = True
            hub_node.setdefault("power", {})["state"] = "on"
            if was_disabled:
                hub_node["muted"] = False
                self._local_audio_reset = True
            system["schemaVersion"] = 6
            changed = True
        settings = system.setdefault("settings", {})
        settings["screensaverSeconds"] = int(settings.get("screensaverSeconds", 120))
        if "soundboardEnabled" not in settings:
            settings["soundboardEnabled"] = True
            changed = True
        if schema_version < 7:
            settings["playbackMode"] = "stereo"
            settings["playbackChannels"] = 2
            system["schemaVersion"] = 7
            changed = True
        mode = str(settings.get("playbackMode", "stereo"))
        if mode not in PLAYBACK_MODES:
            mode = "stereo"
            settings["playbackMode"] = mode
            changed = True
        if int(settings.get("playbackChannels", 0)) != PLAYBACK_MODES[mode]:
            settings["playbackChannels"] = PLAYBACK_MODES[mode]
            changed = True
        playback = system.setdefault("playback", {})
        if "live" not in playback:
            playback.clear()
        defaults = {
            "state": "stopped",
            "title": "",
            "artist": "",
            "album": "",
            "source": "AirPlay 2",
            "client": "",
            "positionSeconds": 0,
            "durationSeconds": 0,
            "positionUpdatedAt": 0,
            "artworkRevision": 0,
            "live": True,
            "transportControl": False,
        }
        for key, value in defaults.items():
            playback.setdefault(key, value)
        for node in self.state["nodes"]:
            audio = node.setdefault("audio", {})
            gain = int(audio.get("outputGainDb", 9))
            if gain not in OUTPUT_GAINS or "outputGainDb" not in audio:
                audio["outputGainDb"] = 9
                changed = True
            power = node.setdefault("power", {})
            power_defaults = {
                "transport": "none" if node.get("local") else "auto",
                "piPowerMode": "always_on",
                "shutdownDelaySeconds": 10,
            }
            for key, value in power_defaults.items():
                if key not in power:
                    power[key] = value
                    changed = True
        if schema_version < 8:
            system["schemaVersion"] = 8
            changed = True
        return changed

    def pairing_active(self) -> bool:
        return time.time() < self.pairing_mode_until

    def start_pairing(self, seconds: int = 120) -> float:
        with self.lock:
            self.pairing_mode_until = time.time() + seconds
            return self.pairing_mode_until

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            now = time.time()
            self._prune_browser_nodes(now)
            value = copy.deepcopy(self.state)
            value["nodes"].extend(copy.deepcopy(list(self.browser_nodes.values())))
            playback_channels = int(value["system"]["settings"].get("playbackChannels", 2))
            for node in value["nodes"]:
                node.pop("agentToken", None)
                if node.get("local"):
                    node["modeCompatible"] = playback_channels == 2
                    if playback_channels != 2:
                        node["online"] = False
                    continue
                if node.get("browserTemporary"):
                    browser_online = now - float(node.get("lastHeartbeatAt", 0)) <= BROWSER_NODE_TIMEOUT
                    snapcast_online = bool(node.get("snapcastConnected"))
                    mode_compatible = playback_channels == 2
                    node["audioReady"] = snapcast_online and mode_compatible
                    node["displayOnline"] = None
                    node["health"] = {
                        "browser": browser_online,
                        "snapcast": snapcast_online,
                        "display": None,
                        "playbackMode": mode_compatible,
                    }
                    node["modeCompatible"] = mode_compatible
                    # A temporary node represents the open Hearth browser tab.
                    # Its streaming player has a separate readiness state and
                    # must not make a present browser look like an offline Pi.
                    node["online"] = browser_online
                    continue
                agent_online = now - float(node.get("lastHeartbeatAt", 0)) <= NODE_HEARTBEAT_TIMEOUT
                snapcast_online = bool(node.get("snapcastConnected"))
                detected = node.get("audio", {}).get("detected", {})
                display_required = bool(detected.get("hdmiConnected"))
                stream_channels = int(detected.get("streamChannels", 2))
                max_channels = int(detected.get("maxPcmChannels", 2))
                mode_compatible = stream_channels == playback_channels and (
                    playback_channels == 2
                    or (bool(detected.get("hdmiConnected")) and max_channels >= playback_channels)
                )
                display_online = (
                    not display_required
                    or now - float(node.get("displayLastSeenAt", 0)) <= DISPLAY_HEARTBEAT_TIMEOUT
                )
                node["displayOnline"] = display_online if display_required else None
                node["health"] = {
                    "agent": agent_online,
                    "snapcast": snapcast_online,
                    "display": display_online if display_required else None,
                    "playbackMode": mode_compatible,
                }
                node["modeCompatible"] = mode_compatible
                node["online"] = agent_online and snapcast_online and display_online and mode_compatible
            value["system"]["serverTime"] = datetime.now(timezone.utc).isoformat()
            value["system"]["pairing"] = {
                "active": self.pairing_active(),
                "expiresAt": self.pairing_mode_until,
            }
            return value

    def refresh_snapcast_audio(self) -> None:
        # State polling must never queue behind an in-flight volume command.
        # The last known values are preferable to stalling the whole UI.
        if SNAPCAST_RPC_LOCK.locked():
            return
        try:
            status = snapcast_rpc("Server.GetStatus", timeout=0.5)
        except (OSError, TimeoutError, ValueError, json.JSONDecodeError):
            return
        reset_client: dict[str, Any] | None = None
        with self.lock:
            self._prune_browser_nodes(time.time())
            groups = status.get("server", {}).get("groups", [])
            connected_web_clients = [
                client
                for group in groups
                for client in group.get("clients", [])
                if client.get("connected") and (
                    str(client.get("host", {}).get("arch", "")).lower() == "web"
                    or "snapweb" in str(client.get("host", {}).get("name", "")).lower()
                )
            ]
            claimed = {
                str(node.get("snapcastClientId", "")).lower().split("#", 1)[0]
                for node in self.browser_nodes.values()
                if node.get("snapcastClientId")
            }
            unmatched = [
                node for node in self.browser_nodes.values()
                if not node.get("snapcastClientId")
            ]
            candidates = [
                client for client in connected_web_clients
                if str(client.get("id", "")).lower().split("#", 1)[0] not in claimed
            ]
            for node in unmatched:
                source_ip = self._normalise_ip(str(node.get("browserSourceIp", "")))
                client = next(
                    (
                        item for item in candidates
                        if self._normalise_ip(str(item.get("host", {}).get("ip", ""))) == source_ip
                    ),
                    None,
                )
                # Some Snapserver builds do not populate host.ip for Web
                # clients. A single unclaimed player is still unambiguous.
                if client is None and len(unmatched) == 1 and len(candidates) == 1:
                    client = candidates[0]
                if client is not None:
                    node["snapcastClientId"] = str(client.get("id", "")).split("#", 1)[0]
                    candidates.remove(client)
            playback_channels = int(self.state["system"]["settings"].get("playbackChannels", 2))
            all_nodes = self.state["nodes"] + list(self.browser_nodes.values())
            for node in all_nodes:
                client = snapclient_for_node(status, node)
                connected = bool(client and client.get("connected"))
                node["snapcastConnected"] = connected
                if connected:
                    node["snapcastLastSeenAt"] = time.time()
                if node.get("local"):
                    node["online"] = connected and playback_channels == 2
                elif node.get("browserTemporary"):
                    heartbeat_fresh = time.time() - float(node.get("lastHeartbeatAt", 0)) <= BROWSER_NODE_TIMEOUT
                    node["online"] = heartbeat_fresh
                    node["audioReady"] = connected and playback_channels == 2
                if not client:
                    continue
                volume = client.get("config", {}).get("volume", {})
                node["volume"] = int(volume.get("percent", node.get("volume", 50)))
                node["muted"] = bool(volume.get("muted", node.get("muted", False)))
                if node.get("local"):
                    node["power"]["state"] = "on" if node.get("enabled", True) else "off"
                    if self._local_audio_reset and connected:
                        node["enabled"] = True
                        node["muted"] = False
                        reset_client = copy.deepcopy(client)
        if reset_client:
            try:
                snapcast_rpc(
                    "Client.SetVolume",
                    {
                        "id": str(reset_client["id"]),
                        "volume": {
                            "muted": False,
                            "percent": max(0, min(100, int(self.node(LOCAL_HUB_NODE_ID).get("volume", 50)))),
                        },
                    },
                )
            except ValueError:
                return
            self._local_audio_reset = False

    def refresh_transport_control(self) -> None:
        now = time.monotonic()
        if now - self._transport_checked_at < 10:
            return
        self._transport_checked_at = now
        try:
            protocol = subprocess.run(
                [
                    "busctl", "--system", "get-property",
                    "org.gnome.ShairportSync", "/org/gnome/ShairportSync",
                    "org.gnome.ShairportSync", "Protocol",
                ],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            result = subprocess.run(
                [
                    "busctl", "--system", "get-property",
                    "org.gnome.ShairportSync", "/org/gnome/ShairportSync",
                    "org.gnome.ShairportSync.RemoteControl", "Available",
                ],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            # Shairport Sync's outgoing DACP transport commands are dependable
            # for Classic AirPlay only. AirPlay 2 may report an interface but
            # silently ignore the command, so do not present a false control.
            protocol_name = re.sub(r"[\s_-]+", "", protocol.stdout.lower())
            is_airplay_2 = protocol.returncode == 0 and "airplay2" in protocol_name
            self._transport_available = (
                not is_airplay_2
                and result.returncode == 0
                and result.stdout.strip().endswith("true")
            )
        except (OSError, subprocess.TimeoutExpired):
            self._transport_available = False
        with self.lock:
            self.state["system"]["playback"]["transportControl"] = self._transport_available

    def apply_snapcast_audio(self, node_id: str) -> None:
        with self.lock:
            node = copy.deepcopy(self.node(node_id))
        # Resolve the live client every time. Snapserver can suffix an id (for
        # example ``#2``) after reconnects, while the host MAC remains stable.
        client = snapclient_for_node(snapcast_rpc("Server.GetStatus"), node)
        if not client or not client.get("connected"):
            raise ValueError(f"{node.get('name', 'This location')}'s Snapclient is not connected yet.")
        snapcast_rpc(
            "Client.SetVolume",
            {
                "id": str(client["id"]),
                "volume": {
                    "muted": bool(node.get("muted")) or not bool(node.get("enabled", True)),
                    "percent": max(0, min(100, int(node.get("volume", 50)))),
                },
            },
        )

    def node(self, node_id: str) -> dict[str, Any]:
        for node in self.state["nodes"]:
            if node["id"] == node_id:
                return node
        if node_id in self.browser_nodes:
            return self.browser_nodes[node_id]
        raise KeyError(node_id)

    @staticmethod
    def _normalise_ip(value: str) -> str:
        value = value.strip().strip("[]")
        if value.lower().startswith("::ffff:"):
            value = value[7:]
        try:
            address = ipaddress.ip_address(value.split("%", 1)[0])
            if getattr(address, "ipv4_mapped", None):
                return str(address.ipv4_mapped)
            return str(address)
        except ValueError:
            return value.lower()

    def _prune_browser_nodes(self, now: float | None = None) -> None:
        current = time.time() if now is None else now
        expired = [
            node_id
            for node_id, node in self.browser_nodes.items()
            if current - float(node.get("lastHeartbeatAt", 0)) > BROWSER_NODE_TIMEOUT
        ]
        for node_id in expired:
            self.browser_nodes.pop(node_id, None)

    def register_browser_node(self, session_id: str, name: str, source_ip: str) -> dict[str, Any]:
        session_id = session_id.strip().lower()
        if not re.fullmatch(r"[a-z0-9_-]{8,80}", session_id):
            raise ValueError("The temporary-node session id is invalid.")
        name = re.sub(r"\s+", " ", name).strip()
        if not name:
            raise ValueError("Enter a name for this temporary node.")
        if len(name) > 48:
            raise ValueError("Temporary node names can be at most 48 characters.")
        with self.lock:
            playback_channels = int(self.state["system"]["settings"].get("playbackChannels", 2))
            if playback_channels != 2:
                raise ValueError(
                    "Temporary browser nodes require Stereo PCM. Switch Whole-home audio to Stereo first."
                )
            node_id = f"browser-{session_id}"
            existing = self.browser_nodes.get(node_id, {})
            node = {
                "id": node_id,
                "name": name,
                "room": "Temporary",
                "hardware": "Web browser · Temporary node",
                "browserTemporary": True,
                # A temporary browser node is present as soon as its page starts
                # heartbeating.  Snapweb's audio connection is tracked separately
                # in audioReady so the card never flashes as offline before Play.
                "online": True,
                "audioReady": False,
                "enabled": True,
                "volume": int(existing.get("volume", 50)),
                "muted": bool(existing.get("muted", False)),
                "snapcastClientId": str(existing.get("snapcastClientId", "")),
                "snapcastConnected": bool(existing.get("snapcastConnected", False)),
                "connection": {"type": "browser", "address": source_ip, "quality": 100},
                "button": {"enabled": False, "status": "not applicable"},
                "wifi": {"supported": False, "status": "managed by this device", "ssid": "", "signal": 0},
                "audio": {
                    "requestedProfile": "browser_stereo",
                    "activeProfile": "browser_stereo",
                    "outputGainDb": 0,
                    "profiles": [{
                        "id": "browser_stereo",
                        "label": "Browser stereo",
                        "available": True,
                        "detail": "Synchronized stereo playback through this browser.",
                    }],
                    "detected": {
                        "analog": False,
                        "hdmiConnected": False,
                        "sink": "Web Audio",
                        "maxPcmChannels": 2,
                        "streamChannels": 2,
                        "formats": ["PCM", "FLAC"],
                    },
                },
                "power": {
                    "controlType": "button",
                    "stateMode": "stateless",
                    "state": "on",
                    "transport": "none",
                    "piPowerMode": "always_on",
                    "shutdownDelaySeconds": 10,
                    "onActionId": "",
                    "offActionId": "",
                    "pressActionId": "",
                },
                "ir": {"fileName": "", "signals": []},
                "actions": [],
                "lastAction": None,
                "agentUrl": "",
                "browserSessionId": session_id,
                "browserSourceIp": source_ip,
                "lastHeartbeatAt": time.time(),
            }
            self.browser_nodes[node_id] = node
            return copy.deepcopy(node)

    def heartbeat_browser_node(self, node_id: str) -> dict[str, Any]:
        with self.lock:
            node = self.browser_nodes[node_id]
            node["lastHeartbeatAt"] = time.time()
            return copy.deepcopy(node)

    def remove_browser_node(self, node_id: str) -> dict[str, Any] | None:
        with self.lock:
            return self.browser_nodes.pop(node_id, None)

    def save(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.state_path)

    def update_system_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        if not {"screensaverSeconds", "playbackMode", "soundboardEnabled"}.intersection(changes):
            raise ValueError("No supported setting was provided.")
        soundboard_enabled = changes.get("soundboardEnabled")
        if soundboard_enabled is not None and type(soundboard_enabled) is not bool:
            raise ValueError("Soundboard setting must be on or off.")
        seconds = None
        if "screensaverSeconds" in changes:
            seconds = int(changes["screensaverSeconds"])
            if seconds not in {0, 30, 60, 120, 300, 600, 1800}:
                raise ValueError("Unsupported screensaver timer.")
        mode = None
        if "playbackMode" in changes:
            mode = str(changes["playbackMode"])
            if mode not in PLAYBACK_MODES:
                raise ValueError("Playback mode must be Stereo, 5.1 PCM, or 7.1 PCM.")
            with self.lock:
                current_mode = str(self.state["system"]["settings"].get("playbackMode", "stereo"))
            if mode != current_mode:
                try:
                    result = subprocess.run(
                        ["sudo", "-n", PLAYBACK_MODE_HELPER, mode],
                        capture_output=True,
                        text=True,
                        timeout=45,
                        check=False,
                    )
                except (OSError, subprocess.TimeoutExpired) as error:
                    raise ValueError(f"Could not switch the synchronized playback mode: {error}") from error
                if result.returncode:
                    raise ValueError(
                        result.stderr.strip() or result.stdout.strip() or "The audio pipeline rejected the playback mode."
                    )
        with self.lock:
            settings = self.state["system"]["settings"]
            if seconds is not None:
                settings["screensaverSeconds"] = seconds
            if mode is not None:
                settings["playbackMode"] = mode
                settings["playbackChannels"] = PLAYBACK_MODES[mode]
            if soundboard_enabled is not None:
                settings["soundboardEnabled"] = soundboard_enabled
                self.soundboard.set_enabled(soundboard_enabled)
            self.save()
            return copy.deepcopy(settings)

    def apply_playback(self, changes: dict[str, object]) -> None:
        with self.lock:
            self.state["system"]["playback"].update(changes)
            if changes.get("artworkRevision") == 0:
                self.artwork = b""
                self.artwork_type = "application/octet-stream"
                if self.artwork_path:
                    self.artwork_path.unlink(missing_ok=True)
                self.save()

    def set_artwork(self, content: bytes, media_type: str) -> None:
        with self.lock:
            self.artwork = content
            self.artwork_type = media_type
            if self.artwork_path:
                self.artwork_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.artwork_path.with_name(self.artwork_path.name + ".tmp")
                temporary.write_bytes(content)
                temporary.replace(self.artwork_path)
            playback = self.state["system"]["playback"]
            playback["artworkRevision"] = int(playback.get("artworkRevision", 0)) + 1
            self.save()

    def update_node(self, node_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        allowed = {"name", "room", "enabled", "volume", "muted"}
        with self.lock:
            node = self.node(node_id)
            for key in allowed:
                if key in changes:
                    node[key] = changes[key]
            if "audioProfile" in changes:
                profile_id = str(changes["audioProfile"])
                profile = next(
                    (item for item in node["audio"]["profiles"] if item["id"] == profile_id),
                    None,
                )
                if not profile:
                    raise ValueError("Unknown audio profile.")
                if not profile["available"]:
                    raise ValueError(profile["detail"])
                node["audio"]["requestedProfile"] = profile_id
                node["audio"]["activeProfile"] = (
                    self._best_auto_profile(node) if profile_id == "auto" else profile_id
                )
            if "outputGainDb" in changes:
                gain = int(changes["outputGainDb"])
                if gain not in OUTPUT_GAINS:
                    raise ValueError("Output gain must be -6, -3, 0, +3, +6, +9, or +12 dB.")
                node["audio"]["outputGainDb"] = gain
            if "power" in changes:
                power = changes["power"]
                if power.get("controlType") not in {"button", "switch"}:
                    raise ValueError("Power control must be a button or switch.")
                if power.get("stateMode") not in {"stateless", "assumed", "confirmed"}:
                    raise ValueError("Unsupported power state mode.")
                if power.get("transport", "auto") not in {"auto", "ir", "hdmi_cec", "none"}:
                    raise ValueError("Power transport must be automatic, IR, HDMI-CEC, or none.")
                if power.get("piPowerMode", "always_on") not in {"always_on", "soft_sleep", "halt"}:
                    raise ValueError("Unsupported Raspberry Pi power mode.")
                delay = int(power.get("shutdownDelaySeconds", 10))
                if delay not in {3, 5, 10, 20, 30, 60}:
                    raise ValueError("Unsupported node shutdown delay.")
                power["shutdownDelaySeconds"] = delay
                node["power"].update(power)
            self.save()
            return copy.deepcopy(node)

    @staticmethod
    def _best_auto_profile(node: dict[str, Any]) -> str:
        preferred = ["hdmi_71_pcm", "hdmi_51_pcm", "hdmi_stereo", "analog_stereo", "i2s_stereo"]
        available = {item["id"] for item in node["audio"]["profiles"] if item["available"]}
        return next((item for item in preferred if item in available), "analog_stereo")


class HubHandler(BaseHTTPRequestHandler):
    store: HubStore
    server_version = "HearthAudio/0.1"

    def log_message(self, message: str, *args: Any) -> None:
        print(f"{self.address_string()} - {message % args}")

    def _json(self, value: Any, status: int = HTTPStatus.OK) -> None:
        payload = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    @staticmethod
    def _node_request(node: dict[str, Any], route: str, body: dict[str, Any]) -> dict[str, Any] | None:
        agent_url = str(node.get("agentUrl", "")).rstrip("/")
        if not agent_url:
            return None
        headers = {"Content-Type": "application/json"}
        if node.get("agentToken"):
            headers["X-Hearth-Token"] = str(node["agentToken"])
        request = urllib.request.Request(
            f"{agent_url}{route}",
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                message = json.loads(exc.read()).get("error", str(exc))
            except Exception:
                message = str(exc)
            raise ValueError(f"Node rejected the command: {message}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ValueError(f"Node is currently unreachable: {exc}") from exc

    def _body(self) -> dict[str, Any]:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid content length.") from exc
        if size > 1_500_000:
            raise ValueError("Request is too large.")
        try:
            value = json.loads(self.rfile.read(size) or b"{}")
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid JSON body.") from exc
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object.")
        return value

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/soundboard":
            self._json(self.store.soundboard.snapshot())
            return
        if self.path == "/api/state":
            self.store.refresh_snapcast_audio()
            self.store.refresh_transport_control()
            self._json(self.store.snapshot())
            return
        if self.path == "/api/health":
            self._json({"ok": True, "time": time.time()})
            return
        if self.path.split("?", 1)[0] == "/api/artwork":
            self._artwork()
            return
        self._serve_static()

    def _artwork(self) -> None:
        with self.store.lock:
            payload = self.store.artwork
            media_type = self.store.artwork_type
        if not payload:
            self._error(HTTPStatus.NOT_FOUND, "No artwork is available.")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _serve_static(self) -> None:
        route = self.path.split("?", 1)[0]
        relative = "index.html" if route in {"", "/"} else route.lstrip("/")
        candidate = (STATIC_ROOT / relative).resolve()
        if STATIC_ROOT.resolve() not in candidate.parents and candidate != STATIC_ROOT.resolve():
            self._error(HTTPStatus.NOT_FOUND, "Not found.")
            return
        if not candidate.is_file():
            candidate = STATIC_ROOT / "index.html"
        payload = candidate.read_bytes()
        media_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{media_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802
        try:
            if self.path == "/api/soundboard/upload":
                self._sound_upload()
                return
            body = self._body()
            if self.path == "/api/soundboard/play":
                self.store.soundboard.enqueue(body.get("soundIds", []), body.get("targetNodeIds", []))
                self._json({"queued": True}, HTTPStatus.ACCEPTED)
                return
            if self.path == "/api/soundboard/stop":
                self.store.soundboard.stop()
                self._json({"stopping": True})
                return
            if self.path == "/api/soundboard/schedules":
                self._json({"schedule": self.store.soundboard.add_schedule(body)}, HTTPStatus.CREATED)
                return
            if self.path == "/api/nodes/register":
                self._register(body)
                return
            if self.path == "/api/browser-nodes/register":
                self._register_browser_node(body)
                return
            if self.path == "/api/settings":
                self._json({"settings": self.store.update_system_settings(body)})
                return
            if self.path == "/api/display/power":
                self._display_power(body)
                return
            if self.path == "/api/pairing/start":
                expires_at = self.store.start_pairing()
                self._json({"active": True, "expiresAt": expires_at})
                return

            transport = re.fullmatch(r"/api/playback/(previous|play-pause|next)", self.path)
            if transport:
                self._transport(transport.group(1))
                return

            display_heartbeat = re.fullmatch(r"/api/nodes/([^/]+)/display/heartbeat", self.path)
            if display_heartbeat:
                self._display_heartbeat(display_heartbeat.group(1))
                return

            browser_node = re.fullmatch(r"/api/browser-nodes/([^/]+)/(heartbeat|unregister)", self.path)
            if browser_node:
                node_id, operation = browser_node.groups()
                if operation == "heartbeat":
                    self._browser_node_heartbeat(node_id)
                else:
                    self._unregister_browser_node(node_id)
                return

            match = re.fullmatch(r"/api/nodes/([^/]+)/(settings|wifi|ir/import|audio/rescan|power|button)", self.path)
            if match:
                node_id, operation = match.groups()
                if operation == "settings":
                    self._settings(node_id, body)
                elif operation == "wifi":
                    self._wifi(node_id, body)
                elif operation == "ir/import":
                    self._import_ir(node_id, body)
                elif operation == "audio/rescan":
                    self._rescan_audio(node_id, body)
                elif operation == "button":
                    self._button(node_id, body)
                else:
                    self._power(node_id, body)
                return

            match = re.fullmatch(r"/api/nodes/([^/]+)/actions(?:/([^/]+)/run)?", self.path)
            if match:
                node_id, action_id = match.groups()
                if action_id:
                    self._run_action(node_id, action_id)
                else:
                    self._save_actions(node_id, body)
                return
            self._error(HTTPStatus.NOT_FOUND, "API route not found.")
        except KeyError:
            self._error(HTTPStatus.NOT_FOUND, "Node not found.")
        except PermissionError as exc:
            self._error(HTTPStatus.FORBIDDEN, str(exc))
        except (ValueError, InfraredFileError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:  # pragma: no cover - final HTTP boundary
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Prototype server error: {exc}")

    def _sound_upload(self) -> None:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid upload size.") from exc
        if not 0 < size <= MAX_UPLOAD:
            raise ValueError("Sound files must be between 1 byte and 25 MB.")
        name = unquote(self.headers.get("X-Filename", ""))
        sound = self.store.soundboard.upload(name, self.rfile.read(size))
        self._json({"sound": sound}, HTTPStatus.CREATED)

    def do_DELETE(self) -> None:  # noqa: N802
        try:
            sound = re.fullmatch(r"/api/soundboard/sounds/([a-f0-9]{32})", self.path)
            schedule = re.fullmatch(r"/api/soundboard/schedules/([a-f0-9]{32})", self.path)
            if sound:
                self.store.soundboard.delete_sound(sound.group(1))
            elif schedule:
                self.store.soundboard.delete_schedule(schedule.group(1))
            else:
                self._error(HTTPStatus.NOT_FOUND, "API route not found.")
                return
            self._json({"deleted": True})
        except KeyError:
            self._error(HTTPStatus.NOT_FOUND, "Sound or schedule not found.")
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))

    def _register(self, body: dict[str, Any]) -> None:
        node_id = str(body.get("id", "")).strip()
        if not node_id:
            raise ValueError("Node id is required.")
        with self.store.lock:
            try:
                node = self.store.node(node_id)
                dynamic_fields = {
                    "hardware", "snapcastClientId", "connection", "wifi",
                    "audio", "agentUrl", "button", "lastAction", "systemPowerState",
                }
                node.update({key: value for key, value in body.items() if key in dynamic_fields})
            except KeyError:
                if not self.store.pairing_active():
                    raise PermissionError("Open Add node on the hub before pairing a new node.")
                node = body | {"online": True, "actions": body.get("actions", []), "ir": body.get("ir", {"fileName": "", "signals": []})}
                self.store.state["nodes"].append(node)
            node["lastHeartbeatAt"] = time.time()
            self.store.save()
        playback_channels = int(self.store.state["system"]["settings"].get("playbackChannels", 2))
        self._json({"registered": True, "node": node, "playbackChannels": playback_channels})

    def _register_browser_node(self, body: dict[str, Any]) -> None:
        node = self.store.register_browser_node(
            str(body.get("sessionId", "")),
            str(body.get("name", "")),
            self.client_address[0],
        )
        self._json({"registered": True, "node": node})

    def _browser_node_heartbeat(self, node_id: str) -> None:
        self.store.heartbeat_browser_node(node_id)
        self._json({"accepted": True})

    def _unregister_browser_node(self, node_id: str) -> None:
        removed = self.store.remove_browser_node(node_id)
        self._json({"removed": bool(removed)})

    def _display_heartbeat(self, node_id: str) -> None:
        with self.store.lock:
            node = self.store.node(node_id)
            node["displayLastSeenAt"] = time.time()
            self.store.save()
        self._json({"accepted": True})

    def _display_power(self, body: dict[str, Any]) -> None:
        state = str(body.get("state", ""))
        if state not in {"on", "off"}:
            raise ValueError("Display power state must be on or off.")
        try:
            local_request = ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            local_request = False
        if not local_request:
            self._json({"accepted": True, "hardware": False, "state": state})
            return
        try:
            result = subprocess.run(
                ["sudo", "-n", DISPLAY_POWER_HELPER, state],
                capture_output=True,
                text=True,
                timeout=4,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValueError(f"Could not change the Hub display power: {error}") from error
        if result.returncode:
            raise ValueError(result.stderr.strip() or result.stdout.strip() or "Display power helper failed.")
        self._json({"accepted": True, "hardware": True, "state": state})

    def _transport(self, operation: str) -> None:
        methods = {"previous": "Previous", "play-pause": "PlayPause", "next": "Next"}
        self.store.refresh_transport_control()
        with self.store.lock:
            available = bool(self.store.state["system"]["playback"].get("transportControl"))
        if not available:
            raise ValueError("The current AirPlay sender does not expose remote transport control.")
        try:
            result = subprocess.run(
                [
                    "dbus-send", "--system", "--print-reply", "--type=method_call",
                    "--dest=org.gnome.ShairportSync", "/org/gnome/ShairportSync",
                    f"org.gnome.ShairportSync.RemoteControl.{methods[operation]}",
                ],
                capture_output=True,
                text=True,
                timeout=4,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise ValueError("AirPlay transport control timed out.") from error
        except OSError as error:
            raise ValueError("AirPlay transport control is not installed on this hub.") from error
        if result.returncode:
            raise ValueError(result.stderr.strip() or result.stdout.strip() or "AirPlay transport command failed.")
        self._json({"accepted": True, "operation": operation})

    def _settings(self, node_id: str, body: dict[str, Any]) -> None:
        with self.store.lock:
            previous_audio = copy.deepcopy(self.store.node(node_id).get("audio", {}))
            previous_profile = str(previous_audio.get("requestedProfile", "auto"))
            previous_gain = int(previous_audio.get("outputGainDb", 9))
        node = self.store.update_node(node_id, body)
        profile_changed = "audioProfile" in body and str(body["audioProfile"]) != previous_profile
        gain_changed = "outputGainDb" in body and int(body["outputGainDb"]) != previous_gain
        if any(key in body for key in {"enabled", "volume", "muted"}):
            self.store.apply_snapcast_audio(node_id)
        try:
            if node_id == LOCAL_HUB_NODE_ID:
                if profile_changed:
                    result = subprocess.run(
                        ["sudo", "-n", "/usr/local/sbin/hearth-snapclient-output", node["audio"]["activeProfile"]],
                        capture_output=True,
                        text=True,
                        timeout=30,
                        check=False,
                    )
                    if result.returncode:
                        raise ValueError(result.stderr.strip() or result.stdout.strip() or "Hub audio output switch failed.")
                if profile_changed or gain_changed:
                    result = subprocess.run(
                        ["sudo", "-n", OUTPUT_GAIN_HELPER, str(node["audio"].get("outputGainDb", 9))],
                        capture_output=True,
                        text=True,
                        timeout=8,
                        check=False,
                    )
                    if result.returncode:
                        raise ValueError(result.stderr.strip() or result.stdout.strip() or "Hub output gain change failed.")
            else:
                for changed, path, payload in (
                    (profile_changed, "/api/v1/audio/profile", {"profile": str(body.get("audioProfile", "auto"))}),
                    (gain_changed, "/api/v1/audio/gain", {"gainDb": int(body.get("outputGainDb", 9))}),
                ):
                    if not changed:
                        continue
                    forwarded = self._node_request(node, path, payload)
                    if forwarded and isinstance(forwarded.get("audio"), dict):
                        with self.store.lock:
                            stored = self.store.node(node_id)
                            stored["audio"] = forwarded["audio"]
                            self.store.save()
                            node = copy.deepcopy(stored)
        except Exception:
            if profile_changed or gain_changed:
                with self.store.lock:
                    stored = self.store.node(node_id)
                    stored["audio"] = previous_audio
                    self.store.save()
            raise
        self._json({"node": node})

    def _wifi(self, node_id: str, body: dict[str, Any]) -> None:
        ssid = str(body.get("ssid", "")).strip()
        if not ssid:
            raise ValueError("Wi-Fi network name is required.")
        with self.store.lock:
            node = self.store.node(node_id)
            if not node["wifi"].get("supported"):
                raise ValueError("This node has no Wi-Fi adapter. Attach one before configuring Wi-Fi.")
            forwarded = self._node_request(node, "/api/v1/wifi", {"ssid": ssid, "password": str(body.get("password", ""))})
            node["wifi"].update({"ssid": ssid, "status": "reconnecting", "signal": 0})
            self.store.save()
        message = forwarded.get("message") if forwarded else "Prototype accepted the network; no live agent is attached."
        self._json({"accepted": True, "message": f"{message} The password was not stored.", "node": node})

    def _import_ir(self, node_id: str, body: dict[str, Any]) -> None:
        content = str(body.get("content", ""))
        signals = [signal.to_dict() for signal in parse_flipper_ir(content)]
        with self.store.lock:
            node = self.store.node(node_id)
            node["ir"] = {"fileName": str(body.get("fileName", "remote.ir")), "signals": signals}
            self.store.save()
        self._json({"imported": len(signals), "signals": signals})

    def _rescan_audio(self, node_id: str, body: dict[str, Any]) -> None:
        with self.store.lock:
            node = self.store.node(node_id)
            if node_id == LOCAL_HUB_NODE_ID:
                requested = node["audio"].get("requestedProfile", "auto")
                node["audio"] = audio_capabilities(hardware_model())
                available = {profile["id"] for profile in node["audio"]["profiles"] if profile["available"]}
                node["audio"]["requestedProfile"] = requested if requested in available else "auto"
                if node["audio"]["requestedProfile"] != "auto":
                    node["audio"]["activeProfile"] = node["audio"]["requestedProfile"]
            else:
                forwarded = self._node_request(node, "/api/v1/audio/rescan", {})
                if forwarded and isinstance(forwarded.get("audio"), dict):
                    node["audio"] = forwarded["audio"]
            detected = body.get("detected")
            if isinstance(detected, dict):
                node["audio"]["detected"].update(detected)
            node["audio"]["lastScan"] = datetime.now(timezone.utc).isoformat()
            self.store.save()
        self._json({"node": node, "message": "Capability rescan requested."})

    def _save_actions(self, node_id: str, body: dict[str, Any]) -> None:
        actions = body.get("actions")
        if not isinstance(actions, list):
            raise ValueError("Actions must be a list.")
        for action in actions:
            if not action.get("id") or not action.get("name") or not isinstance(action.get("steps"), list):
                raise ValueError("Each action requires id, name, and steps.")
        with self.store.lock:
            node = self.store.node(node_id)
            node["actions"] = actions
            self.store.save()
        self._json({"saved": len(actions), "actions": actions})

    def _run_action(self, node_id: str, action_id: str) -> None:
        action = self._queue_action(node_id, action_id)
        self._json({"queued": True, "action": action, "message": "Prototype queued the action for the node."}, HTTPStatus.ACCEPTED)

    def _queue_action(self, node_id: str, action_id: str) -> dict[str, Any]:
        with self.store.lock:
            node = self.store.node(node_id)
            action = next((item for item in node["actions"] if item["id"] == action_id), None)
            if not action:
                raise ValueError("Action not found.")
            self._node_request(node, "/api/v1/actions/run", {"action": action, "signals": node["ir"]["signals"]})
            node["lastAction"] = {"id": action_id, "status": "queued", "at": datetime.now(timezone.utc).isoformat()}
            self.store.save()
        return action

    def _button(self, node_id: str, body: dict[str, Any]) -> None:
        event = str(body.get("event", ""))
        if event not in {"short", "long", "very_long"}:
            raise ValueError("Button event must be short, long, or very_long.")
        with self.store.lock:
            node = self.store.node(node_id)
            expected = str(node.get("agentToken", ""))
            if expected and self.headers.get("X-Hearth-Token", "") != expected:
                self._error(HTTPStatus.UNAUTHORIZED, "Invalid node pairing token.")
                return
            node.setdefault("button", {})["lastEvent"] = {
                "type": event,
                "durationSeconds": float(body.get("durationSeconds", 0)),
                "at": datetime.now(timezone.utc).isoformat(),
            }
            action_id = None
            message = "Button event recorded."
            if event == "short":
                power = node["power"]
                if power["controlType"] == "button":
                    action_id = power.get("pressActionId")
                elif power.get("state") == "on":
                    action_id = power.get("offActionId")
                    power["state"] = "off"
                else:
                    action_id = power.get("onActionId")
                    power["state"] = "on"
                message = "Short press accepted."
            elif event == "long":
                node["button"]["pairingModeUntil"] = time.time() + 120
                message = "Pairing mode enabled for two minutes."
            else:
                node["button"]["resetRequested"] = True
                message = "Reset requested but not executed; confirmation is required."
            self.store.save()
        if action_id:
            self._queue_action(node_id, action_id)
            message += " Assigned power action queued."
        elif event == "short":
            message += " No power action is assigned yet."
        self._json({"accepted": True, "event": event, "message": message}, HTTPStatus.ACCEPTED)

    def _power(self, node_id: str, body: dict[str, Any]) -> None:
        requested = str(body.get("state", "press"))
        if requested not in {"on", "off", "press"}:
            raise ValueError("Power state must be on, off, or press.")
        if node_id == LOCAL_HUB_NODE_ID:
            with self.store.lock:
                node = self.store.node(node_id)
                if requested == "press":
                    requested = "off" if node.get("enabled", True) else "on"
                enabled = requested != "off"
                node["enabled"] = enabled
                node["muted"] = not enabled
                node["power"]["state"] = "on" if enabled else "off"
                self.store.save()
            self.store.apply_snapcast_audio(node_id)
            self._json({"accepted": True, "state": "on" if enabled else "off"})
            return
        with self.store.lock:
            node = self.store.node(node_id)
            power = node["power"]
            transport = str(power.get("transport", "auto"))
            pi_power_mode = str(power.get("piPowerMode", "always_on"))
            shutdown_delay = int(power.get("shutdownDelaySeconds", 10))
            system_power_state = str(node.get("systemPowerState", "awake"))
            target_state = requested
            if requested == "press" and power["controlType"] == "button" and pi_power_mode in {"soft_sleep", "halt"}:
                target_state = "on" if system_power_state in {"sleeping", "halting"} else "off"
            elif requested == "press":
                target_state = "off" if power.get("state") == "on" else "on"
            if power["controlType"] == "button":
                action_id = power.get("pressActionId")
            elif target_state == "on":
                action_id = power.get("onActionId")
                if power["stateMode"] != "stateless":
                    power["state"] = "on"
            else:
                action_id = power.get("offActionId")
                if power["stateMode"] != "stateless":
                    power["state"] = "off"
            self.store.save()

        if target_state == "on" and pi_power_mode == "halt" and system_power_state == "halting":
            raise ValueError(
                "This node is halted and cannot wake over the network. Press its GPIO3 wake button or use an external power controller."
            )
        if target_state == "on" and pi_power_mode == "soft_sleep":
            self._node_request(node, "/api/v1/system/power", {"state": "wake", "delaySeconds": 0})
            with self.store.lock:
                self.store.node(node_id)["systemPowerState"] = "awake"
                self.store.save()

        hdmi_connected = bool(node.get("audio", {}).get("detected", {}).get("hdmiConnected"))
        device_controlled = False
        if transport in {"auto", "ir"} and action_id:
            self._queue_action(node_id, str(action_id))
            device_controlled = True
        elif transport in {"auto", "hdmi_cec"} and hdmi_connected:
            self._node_request(node, "/api/v1/cec/power", {"state": target_state})
            device_controlled = True
        elif transport not in {"none", "auto", "ir", "hdmi_cec"}:
            raise ValueError("Unsupported connected-device power transport.")

        if target_state == "off" and pi_power_mode in {"soft_sleep", "halt"}:
            next_state = "sleep" if pi_power_mode == "soft_sleep" else "halt"
            forwarded = self._node_request(
                node,
                "/api/v1/system/power",
                {"state": next_state, "delaySeconds": shutdown_delay},
            )
            with self.store.lock:
                self.store.node(node_id)["systemPowerState"] = str(
                    (forwarded or {}).get("state", "sleeping" if next_state == "sleep" else "halting")
                )
                self.store.save()

        if not device_controlled and pi_power_mode == "always_on":
            if transport == "ir":
                raise ValueError("No IR action is assigned to this power control.")
            if transport == "hdmi_cec":
                raise ValueError("HDMI-CEC was selected, but no HDMI receiver is detected.")
            raise ValueError("No connected-device power method is currently available.")

        with self.store.lock:
            stored = self.store.node(node_id)
            if stored["power"].get("stateMode") != "stateless" or transport == "hdmi_cec":
                stored["power"]["state"] = target_state
            self.store.save()
        self._json(
            {
                "accepted": True,
                "state": target_state,
                "deviceControlled": device_controlled,
                "piPowerMode": pi_power_mode,
            },
            HTTPStatus.ACCEPTED,
        )


def make_server(
    host: str,
    port: int,
    state_path: Path | None = None,
    metadata_pipe: Path | None = None,
    discovery_port: int | None = None,
) -> ThreadingHTTPServer:
    handler = type("ConfiguredHubHandler", (HubHandler,), {"store": HubStore(state_path)})
    server = ThreadingHTTPServer((host, port), handler)
    if metadata_pipe:
        ShairportMetadataReader(handler.store, metadata_pipe).start()
    if discovery_port:
        start_discovery_responder(handler.store, port, discovery_port)
    return server


def start_discovery_responder(store: HubStore, http_port: int, discovery_port: int = DISCOVERY_PORT) -> threading.Thread:
    """Answer node broadcasts only while the hub pairing window is open."""

    def respond() -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("0.0.0.0", discovery_port))
            while True:
                payload, address = listener.recvfrom(1024)
                if payload.strip() != DISCOVERY_REQUEST or not store.pairing_active():
                    continue
                response = json.dumps(
                    {"service": "hearth-hub", "port": http_port, "expiresAt": store.pairing_mode_until}
                ).encode("utf-8")
                listener.sendto(response, address)

    thread = threading.Thread(target=respond, name="hearth-discovery", daemon=True)
    thread.start()
    return thread


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Hearth Audio prototype hub.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", default=8080, type=int)
    parser.add_argument("--state", type=Path, default=ROOT / "runtime" / "state.json")
    parser.add_argument("--metadata-pipe", type=Path, default=Path("/tmp/shairport-sync-metadata"))
    args = parser.parse_args()
    server = make_server(args.host, args.port, args.state, args.metadata_pipe, DISCOVERY_PORT)
    server.RequestHandlerClass.store.soundboard.start()
    print(f"Hearth Audio hub listening at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
