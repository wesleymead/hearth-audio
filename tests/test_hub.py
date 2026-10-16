import json
import copy
import subprocess
import threading
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

from hub.server import (
    BROWSER_NODE_TIMEOUT,
    HubHandler,
    LOCAL_HUB_NODE_ID,
    HubStore,
    local_snapclient,
    make_server,
    snapcast_rpc,
)

from tests.test_flipper_ir import SAMPLE


TEST_NODE = {
    "id": "test-pi2",
    "name": "Test Room",
    "room": "Test Room",
    "hardware": "Raspberry Pi 2 Model B",
    "snapcastClientId": "b8:27:eb:99:cc:67",
    "online": True,
    "enabled": True,
    "volume": 50,
    "muted": False,
    "connection": {"type": "ethernet", "address": "127.0.0.1", "quality": 100},
    "button": {"enabled": True, "gpio": 27, "physicalPin": 13, "groundPin": 14, "pullUp": True, "status": "ready", "lastEvent": None},
    "wifi": {"supported": False, "status": "unavailable", "ssid": "", "signal": 0},
    "audio": {
        "requestedProfile": "auto",
        "activeProfile": "analog_stereo",
        "profiles": [
            {"id": "auto", "label": "Auto", "available": True, "detail": "Best available PCM output."},
            {"id": "analog_stereo", "label": "3.5 mm stereo", "available": True, "detail": "Stereo output."},
            {"id": "hdmi_atmos_direct", "label": "Atmos", "available": False, "detail": "Unavailable on Pi 2."},
        ],
        "detected": {"analog": True, "hdmiConnected": False, "sink": "None", "maxPcmChannels": 2, "formats": ["PCM"]},
    },
    "power": {"controlType": "button", "stateMode": "stateless", "state": "unknown", "onActionId": "", "offActionId": "", "pressActionId": "power-toggle"},
    "ir": {"fileName": "", "signals": []},
    "actions": [{"id": "power-toggle", "name": "Power", "steps": []}],
    "lastAction": None,
}


class HubStoreTests(unittest.TestCase):
    @patch("hub.server.urllib.request.urlopen")
    def test_snapcast_rpc_uses_one_shot_http(self, urlopen):
        def respond(request, timeout):
            payload = json.loads(request.data)
            response = MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(
                # HTTP is one-shot, so even servers that normalize the id type
                # cannot interleave an unrelated notification or response.
                {"id": str(payload["id"]), "jsonrpc": "2.0", "result": {"major": 2}}
            ).encode()
            return response

        urlopen.side_effect = respond
        result = snapcast_rpc("Server.GetRPCVersion")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:1780/jsonrpc")
        self.assertEqual(result, {"major": 2})

    def test_auto_profile_prefers_available_pcm(self):
        store = HubStore()
        store.state["nodes"].append(copy.deepcopy(TEST_NODE))
        node = store.update_node("test-pi2", {"audioProfile": "auto"})
        self.assertEqual(node["audio"]["activeProfile"], "analog_stereo")

    def test_unavailable_atmos_is_rejected(self):
        store = HubStore()
        store.state["nodes"].append(copy.deepcopy(TEST_NODE))
        with self.assertRaisesRegex(ValueError, "Unavailable on Pi 2"):
            store.update_node("test-pi2", {"audioProfile": "hdmi_atmos_direct"})

    def test_default_playback_is_live_and_not_demo_content(self):
        snapshot = HubStore().snapshot()
        playback = snapshot["system"]["playback"]
        self.assertTrue(playback["live"])
        self.assertEqual(playback["state"], "stopped")
        self.assertEqual(playback["title"], "")
        self.assertEqual([node["id"] for node in snapshot["nodes"]], [LOCAL_HUB_NODE_ID])

    def test_soundboard_system_switch_is_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = HubStore(path)
            store.update_system_settings({"soundboardEnabled": False})
            self.assertFalse(HubStore(path).soundboard.snapshot()["enabled"])
            self.assertFalse(HubStore(path).snapshot()["system"]["settings"]["soundboardEnabled"])

    def test_artwork_survives_hub_restart_and_clears_with_playback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            content = b"\x89PNG\r\n\x1a\nhearth-test-image"
            store = HubStore(path)
            store.set_artwork(content, "image/png")

            restored = HubStore(path)
            self.assertEqual(restored.artwork, content)
            self.assertEqual(restored.artwork_type, "image/png")
            self.assertGreater(restored.state["system"]["playback"]["artworkRevision"], 0)

            restored.apply_playback({"artworkRevision": 0})
            self.assertEqual(restored.artwork, b"")
            self.assertFalse(path.with_suffix(".artwork").exists())

    def test_local_hub_audio_settings_are_persisted(self):
        store = HubStore()
        node = store.update_node(LOCAL_HUB_NODE_ID, {"volume": 37, "muted": True})
        self.assertEqual(node["volume"], 37)
        self.assertTrue(node["muted"])

    def test_output_gain_is_validated_and_persisted(self):
        store = HubStore()
        store.state["nodes"].append(copy.deepcopy(TEST_NODE))
        node = store.update_node("test-pi2", {"outputGainDb": -3})
        self.assertEqual(node["audio"]["outputGainDb"], -3)
        node = store.update_node("test-pi2", {"outputGainDb": 12})
        self.assertEqual(node["audio"]["outputGainDb"], 12)
        with self.assertRaisesRegex(ValueError, "Output gain"):
            store.update_node("test-pi2", {"outputGainDb": 2})

    @patch("hub.server.subprocess.run")
    def test_surround_mode_uses_transactional_helper_and_disables_local_output(self, process):
        process.return_value = subprocess.CompletedProcess([], 0, "surround_51 (6 channels)\n", "")
        store = HubStore()
        settings = store.update_system_settings({"playbackMode": "surround_51"})
        self.assertEqual(settings["playbackChannels"], 6)
        self.assertEqual(process.call_args.args[0], [
            "sudo", "-n", "/usr/local/sbin/hearth-playback-mode", "surround_51"
        ])
        self.assertFalse(store.snapshot()["nodes"][0]["online"])

    @patch("hub.server.subprocess.run")
    def test_failed_surround_switch_keeps_previous_mode(self, process):
        process.return_value = subprocess.CompletedProcess([], 1, "", "probe failed")
        store = HubStore()
        with self.assertRaisesRegex(ValueError, "probe failed"):
            store.update_system_settings({"playbackMode": "surround_71"})
        self.assertEqual(store.state["system"]["settings"]["playbackMode"], "stereo")

    def test_surround_node_is_online_only_after_agent_matches_channel_mode(self):
        store = HubStore()
        store.state["system"]["settings"].update({"playbackMode": "surround_51", "playbackChannels": 6})
        node = copy.deepcopy(TEST_NODE)
        node["lastHeartbeatAt"] = time.time()
        node["displayLastSeenAt"] = time.time()
        node["snapcastConnected"] = True
        node["audio"]["detected"].update({"hdmiConnected": True, "maxPcmChannels": 8, "streamChannels": 6})
        store.state["nodes"].append(node)
        self.assertTrue(store.snapshot()["nodes"][-1]["online"])
        node["audio"]["detected"]["streamChannels"] = 2
        self.assertFalse(store.snapshot()["nodes"][-1]["online"])

    def test_local_snapclient_is_found_by_host_id(self):
        client = {"id": LOCAL_HUB_NODE_ID, "connected": True, "host": {"name": "hearth-hub"}}
        status = {"server": {"groups": [{"clients": [client]}]}}
        self.assertIs(local_snapclient(status), client)

    def test_local_snapclient_is_found_by_loopback_after_id_change(self):
        client = {"id": "unexpected#2", "connected": True, "host": {"name": "raspberrypi", "ip": "127.0.0.1"}}
        status = {"server": {"groups": [{"clients": [client]}]}}
        self.assertIs(local_snapclient(status), client)

    def test_connected_local_snapclient_wins_over_stale_duplicate(self):
        stale = {"id": LOCAL_HUB_NODE_ID, "connected": False, "host": {"name": "hearth-hub"}}
        live = {"id": f"{LOCAL_HUB_NODE_ID}#2", "connected": True, "host": {"name": "hearth-hub"}}
        status = {"server": {"groups": [{"clients": [stale, live]}]}}
        self.assertIs(local_snapclient(status), live)

    def test_existing_hub_state_is_always_marked_local(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = json.loads((Path(__file__).parents[1] / "hub" / "default_state.json").read_text())
            state["nodes"][0].pop("local")
            path.write_text(json.dumps(state), encoding="utf-8")
            hub = HubStore(path).node(LOCAL_HUB_NODE_ID)
            self.assertTrue(hub["local"])

    def test_legacy_hub_power_off_state_is_migrated_to_audio_on(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = json.loads((Path(__file__).parents[1] / "hub" / "default_state.json").read_text())
            state["system"]["schemaVersion"] = 5
            state["nodes"][0]["enabled"] = False
            state["nodes"][0]["muted"] = True
            state["nodes"][0]["power"]["state"] = "off"
            path.write_text(json.dumps(state), encoding="utf-8")
            store = HubStore(path)
            hub = store.node(LOCAL_HUB_NODE_ID)
            self.assertTrue(hub["enabled"])
            self.assertFalse(hub["muted"])
            self.assertEqual(hub["power"]["state"], "on")
            self.assertTrue(store._local_audio_reset)

    @patch("hub.server.subprocess.run")
    def test_airplay2_does_not_claim_working_transport_controls(self, process):
        process.side_effect = [
            subprocess.CompletedProcess([], 0, 's "AirPlay 2"\n', ""),
            subprocess.CompletedProcess([], 0, "b true\n", ""),
        ]
        store = HubStore()
        store.refresh_transport_control()
        self.assertFalse(store.state["system"]["playback"]["transportControl"])

    def test_hdmi_node_requires_agent_audio_and_display_heartbeats(self):
        store = HubStore()
        node = copy.deepcopy(TEST_NODE)
        node["audio"]["detected"]["hdmiConnected"] = True
        node["lastHeartbeatAt"] = time.time()
        node["snapcastConnected"] = True
        store.state["nodes"].append(node)
        self.assertFalse(store.snapshot()["nodes"][-1]["online"])
        node["displayLastSeenAt"] = time.time()
        self.assertTrue(store.snapshot()["nodes"][-1]["online"])

    @patch("hub.server.snapcast_rpc")
    def test_remote_volume_uses_snapcast_client(self, rpc):
        store = HubStore()
        store.state["nodes"].append(copy.deepcopy(TEST_NODE))
        client = {
            "id": "b8:27:eb:99:cc:67#2",
            "connected": True,
            "host": {"name": "test-pi2", "mac": "b8:27:eb:99:cc:67"},
            "config": {"volume": {"muted": False, "percent": 50}},
        }
        rpc.side_effect = [
            {"server": {"groups": [{"clients": [client]}]}},
            {"volume": {"muted": True, "percent": 31}},
        ]
        store.update_node("test-pi2", {"volume": 31, "muted": True})
        store.apply_snapcast_audio("test-pi2")
        self.assertEqual(rpc.call_args_list[-1].args[0], "Client.SetVolume")
        self.assertEqual(rpc.call_args_list[-1].args[1]["id"], "b8:27:eb:99:cc:67#2")
        self.assertEqual(rpc.call_args_list[-1].args[1]["volume"], {"muted": True, "percent": 31})

    def test_old_demo_rooms_are_removed_but_real_nodes_remain(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = json.loads((Path(__file__).parents[1] / "hub" / "default_state.json").read_text())
            state["system"].pop("schemaVersion")
            demo = copy.deepcopy(TEST_NODE)
            demo["id"] = "living-room-pi2"
            state["nodes"] = [demo, copy.deepcopy(TEST_NODE)]
            path.write_text(json.dumps(state), encoding="utf-8")
            nodes = HubStore(path).snapshot()["nodes"]
            self.assertEqual([node["id"] for node in nodes], [LOCAL_HUB_NODE_ID, "test-pi2"])

    def test_pairing_window_is_explicit(self):
        store = HubStore()
        self.assertFalse(store.pairing_active())
        store.start_pairing()
        self.assertTrue(store.pairing_active())

    def test_browser_node_is_runtime_only_and_expires_without_heartbeats(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = HubStore(path)
            node = store.register_browser_node("12345678-test", "Patio phone", "192.168.1.50")
            self.assertTrue(node["browserTemporary"])
            self.assertTrue(node["online"])
            self.assertFalse(node["audioReady"])
            browser = store.snapshot()["nodes"][-1]
            self.assertEqual(browser["name"], "Patio phone")
            self.assertTrue(browser["online"])
            self.assertFalse(browser["audioReady"])
            store.save()
            self.assertNotIn(node["id"], [item["id"] for item in HubStore(path).snapshot()["nodes"]])

            store.browser_nodes[node["id"]]["lastHeartbeatAt"] = time.time() - BROWSER_NODE_TIMEOUT - 1
            self.assertNotIn(node["id"], [item["id"] for item in store.snapshot()["nodes"]])

    @patch("hub.server.snapcast_rpc")
    def test_browser_node_claims_connected_snapweb_client_from_same_device(self, rpc):
        store = HubStore()
        node = store.register_browser_node("12345678-web", "Laptop", "192.168.1.51")
        client = {
            "id": "snapweb-client-id",
            "connected": True,
            "host": {"arch": "web", "name": "Snapweb client", "ip": "192.168.1.51"},
            "config": {"volume": {"muted": False, "percent": 64}},
        }
        rpc.return_value = {"server": {"groups": [{"clients": [client]}]}}
        store.refresh_snapcast_audio()
        browser = store.node(node["id"])
        self.assertEqual(browser["snapcastClientId"], "snapweb-client-id")
        self.assertTrue(browser["snapcastConnected"])
        self.assertEqual(browser["volume"], 64)
        self.assertTrue(store.snapshot()["nodes"][-1]["online"])


class HubHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server("127.0.0.1", 0, None)
        cls.server.RequestHandlerClass.store.state["nodes"].append(copy.deepcopy(TEST_NODE))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.base + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method="GET" if body is None else "POST",
        )
        # These integration-style requests run on every deployment target,
        # including the 512 MB Pi Zero 2 W. Its first Python request can take
        # several seconds while Chromium and audio services are active.
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())

    def test_state_does_not_expose_agent_token(self):
        _, state = self.request("/api/state")
        self.assertEqual(len(state["nodes"]), 2)
        self.assertTrue(all("agentToken" not in node for node in state["nodes"]))

    def test_soundboard_upload_and_schedule_http(self):
        request = urllib.request.Request(
            self.base + "/api/soundboard/upload", data=b"sample audio",
            headers={"Content-Type": "application/octet-stream", "X-Filename": "bell.mp3"}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            sound = json.loads(response.read())["sound"]
            self.assertEqual(response.status, 201)
        status, schedule = self.request("/api/soundboard/schedules", {
            "soundIds": [sound["id"]], "targetNodeIds": ["hearth-hub"],
            "time": "09:15", "days": [0, 2], "expiresOn": "2030-12-31",
        })
        self.assertEqual(status, 201)
        status, board = self.request("/api/soundboard")
        self.assertEqual(board["sounds"][0]["name"], "bell.mp3")
        self.assertEqual(board["schedules"][0]["id"], schedule["schedule"]["id"])
        delete = urllib.request.Request(self.base + "/api/soundboard/sounds/" + sound["id"], method="DELETE")
        with urllib.request.urlopen(delete, timeout=10) as response:
            self.assertEqual(response.status, 200)

    def test_imports_flipper_file(self):
        status, result = self.request(
            "/api/nodes/test-pi2/ir/import",
            {"fileName": "receiver.ir", "content": SAMPLE},
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["imported"], 2)

    def test_pi2_wifi_change_returns_explanation(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/nodes/test-pi2/wifi", {"ssid": "New WiFi", "password": "secret"})
        self.assertEqual(error.exception.code, 400)
        payload = json.loads(error.exception.read())
        self.assertIn("no Wi-Fi adapter", payload["error"])

    def test_short_button_press_queues_power_action(self):
        status, result = self.request(
            "/api/nodes/test-pi2/button",
            {"event": "short", "durationSeconds": 0.18},
        )
        self.assertEqual(status, 202)
        self.assertIn("power action queued", result["message"])
        _, state = self.request("/api/state")
        node = next(item for item in state["nodes"] if item["id"] == "test-pi2")
        self.assertEqual(node["button"]["lastEvent"]["type"], "short")
        self.assertEqual(node["lastAction"]["id"], "power-toggle")

    def test_screensaver_setting_is_saved(self):
        status, result = self.request("/api/settings", {"screensaverSeconds": 300})
        self.assertEqual(status, 200)
        self.assertEqual(result["settings"]["screensaverSeconds"], 300)

    def test_display_heartbeat_is_recorded(self):
        status, _ = self.request("/api/nodes/test-pi2/display/heartbeat", {})
        self.assertEqual(status, 200)
        node = self.server.RequestHandlerClass.store.node("test-pi2")
        self.assertGreater(node["displayLastSeenAt"], 0)

    def test_temporary_browser_node_http_lifecycle(self):
        status, result = self.request(
            "/api/browser-nodes/register",
            {"sessionId": "12345678-http", "name": "Test browser"},
        )
        node_id = result["node"]["id"]
        try:
            self.assertEqual(status, 200)
            self.assertTrue(self.server.RequestHandlerClass.store.node(node_id)["browserTemporary"])
            status, _ = self.request(f"/api/browser-nodes/{node_id}/heartbeat", {})
            self.assertEqual(status, 200)
        finally:
            status, result = self.request(f"/api/browser-nodes/{node_id}/unregister", {})
            self.assertEqual(status, 200)
            self.assertTrue(result["removed"])

    @patch.object(HubHandler, "_node_request", return_value={"accepted": True, "state": "sleeping"})
    def test_power_button_can_soft_sleep_a_node_without_losing_hub_wake(self, forward):
        store = self.server.RequestHandlerClass.store
        node = store.node("test-pi2")
        node["systemPowerState"] = "awake"
        node["power"].update({
            "transport": "ir",
            "piPowerMode": "soft_sleep",
            "shutdownDelaySeconds": 10,
            "controlType": "button",
        })
        status, result = self.request("/api/nodes/test-pi2/power", {"state": "press"})
        self.assertEqual(status, 202)
        self.assertEqual(result["state"], "off")
        self.assertEqual(result["piPowerMode"], "soft_sleep")
        power_calls = [call for call in forward.call_args_list if call.args[1] == "/api/v1/system/power"]
        self.assertEqual(len(power_calls), 1)
        self.assertEqual(power_calls[0].args[2], {"state": "sleep", "delaySeconds": 10})
        self.assertEqual(store.node("test-pi2")["systemPowerState"], "sleeping")


if __name__ == "__main__":
    unittest.main()
