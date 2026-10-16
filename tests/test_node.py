import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from node.agent import NodeRuntime, audio_capabilities, classify_button_press


class NodeCapabilityTests(unittest.TestCase):
    @patch("node.agent.snapclient_soundcard", return_value="null")
    @patch("node.agent._edid_decode", return_value=(True, "Integra AVR", 2, ["PCM"]))
    def test_hdmi_reconnect_restores_audio_from_silent_standby(self, _decode, _soundcard):
        runtime = NodeRuntime({"id": "node", "audioProfile": "auto", "allowAudioOutputChanges": True})
        with patch.object(runtime, "apply_audio_profile") as apply:
            runtime.recover_hdmi_audio()
        apply.assert_called_once_with("auto")

    def test_button_press_thresholds(self):
        self.assertEqual(classify_button_press(0.2, 5.0, 15.0), "short")
        self.assertEqual(classify_button_press(5.0, 5.0, 15.0), "long")
        self.assertEqual(classify_button_press(15.0, 5.0, 15.0), "very_long")

    @patch("node.agent._edid_decode", return_value=(False, "No HDMI receiver detected", 2, ["PCM"]))
    def test_original_pi_zero_without_hdmi_reports_no_output(self, _decode):
        audio = audio_capabilities("Raspberry Pi Zero W Rev 1.1")
        profiles = {profile["id"]: profile for profile in audio["profiles"]}
        self.assertEqual(audio["activeProfile"], "no_output")
        self.assertFalse(profiles["auto"]["available"])
        self.assertEqual(profiles["no_output"]["label"], "No Output")

    @patch("node.agent._edid_decode", return_value=(True, "Denon AVR", 8, ["PCM", "Dolby TrueHD"]))
    def test_stereo_mode_only_exposes_stereo_profiles(self, _decode):
        audio = audio_capabilities("Raspberry Pi 2 Model B Rev 1.1")
        profiles = {profile["id"]: profile for profile in audio["profiles"]}
        self.assertTrue(profiles["hdmi_stereo"]["available"])
        self.assertFalse(profiles["hdmi_51_pcm"]["available"])
        self.assertFalse(profiles["hdmi_71_pcm"]["available"])
        self.assertFalse(profiles["hdmi_atmos_direct"]["available"])
        self.assertFalse(profiles["hdmi_dts_direct"]["available"])
        self.assertFalse(profiles["hdmi_dts_hd_direct"]["available"])
        self.assertFalse(profiles["avr_dts_neo"]["available"])
        self.assertEqual(audio["detected"]["streamChannels"], 2)

    @patch("node.agent._edid_decode", return_value=(True, "Denon AVR", 8, ["PCM"]))
    def test_surround_modes_expose_only_matching_verified_hdmi_profile(self, _decode):
        audio_51 = audio_capabilities("Raspberry Pi Zero 2 W Rev 1.0", 6)
        profiles_51 = {profile["id"]: profile for profile in audio_51["profiles"]}
        self.assertTrue(profiles_51["hdmi_51_pcm"]["available"])
        self.assertFalse(profiles_51["hdmi_stereo"]["available"])
        self.assertFalse(profiles_51["hdmi_71_pcm"]["available"])
        self.assertEqual(audio_51["activeProfile"], "hdmi_51_pcm")
        self.assertTrue(audio_51["detected"]["surroundSynchronized"])

        audio_71 = audio_capabilities("Raspberry Pi Zero 2 W Rev 1.0", 8)
        profiles_71 = {profile["id"]: profile for profile in audio_71["profiles"]}
        self.assertTrue(profiles_71["hdmi_71_pcm"]["available"])
        self.assertFalse(profiles_71["hdmi_51_pcm"]["available"])

    @patch("node.agent._edid_decode", return_value=(True, "Denon AVR", 8, ["PCM"]))
    def test_node_restores_previous_stereo_profile_after_surround(self, _decode):
        runtime = NodeRuntime({"id": "node", "audioProfile": "auto", "streamChannels": 2})
        runtime.apply_stream_channels(8)
        self.assertEqual(runtime.audio["activeProfile"], "hdmi_71_pcm")
        self.assertEqual(runtime.config["stereoAudioProfile"], "auto")
        runtime.apply_stream_channels(2)
        self.assertEqual(runtime.audio["detected"]["streamChannels"], 2)
        self.assertEqual(runtime.audio["requestedProfile"], "auto")
        self.assertNotIn("stereoAudioProfile", runtime.config)

    @patch("node.agent.Path.exists", return_value=True)
    @patch("node.agent.run")
    @patch("node.agent._edid_decode", return_value=(True, "Denon AVR", 8, ["PCM"]))
    @patch("node.agent.hardware_model", return_value="Raspberry Pi 2 Model B Rev 1.1")
    def test_dry_run_never_invokes_installed_audio_helper(self, _model, _decode, command, _exists):
        runtime = NodeRuntime({"id": "node", "allowSystemChanges": False})
        runtime.apply_audio_profile("hdmi_stereo")
        command.assert_not_called()

    @patch("node.agent.Path.exists", return_value=True)
    @patch("node.agent.run")
    @patch("node.agent._edid_decode", return_value=(True, "Denon AVR", 8, ["PCM"]))
    @patch("node.agent.hardware_model", return_value="Raspberry Pi 2 Model B Rev 1.1")
    def test_audio_permission_invokes_exact_output_helper(self, _model, _decode, command, _exists):
        command.return_value = MagicMock(returncode=0, stdout="plughw:CARD=vc4hdmi,DEV=0\n", stderr="")
        runtime = NodeRuntime({"id": "node", "allowAudioOutputChanges": True})
        runtime.apply_audio_profile("hdmi_stereo")
        self.assertEqual(command.call_args_list[0], unittest.mock.call(
            ["sudo", "-n", str(Path("/usr/local/sbin/hearth-snapclient-output")), "hdmi_stereo"],
            timeout=30,
        ))
        self.assertEqual(command.call_args_list[1], unittest.mock.call(
            ["sudo", "-n", str(Path("/usr/local/sbin/hearth-snapclient-gain")), "9"],
            timeout=8,
        ))

    @patch("node.agent.Path.exists", return_value=True)
    @patch("node.agent.run")
    @patch("node.agent.hardware_model", return_value="Raspberry Pi Zero 2 W Rev 1.0")
    def test_output_gain_is_discrete_and_persisted(self, _model, command, _exists):
        command.return_value = MagicMock(returncode=0, stdout="+3 dB\n", stderr="")
        runtime = NodeRuntime({"id": "node", "allowAudioOutputChanges": True})
        runtime.apply_output_gain(3)
        self.assertEqual(runtime.audio["outputGainDb"], 3)
        command.assert_called_once_with(
            ["sudo", "-n", str(Path("/usr/local/sbin/hearth-snapclient-gain")), "3"],
            timeout=8,
        )
        with self.assertRaisesRegex(ValueError, "must be"):
            runtime.apply_output_gain(2)

    @patch("node.agent._edid_decode", return_value=(True, "Denon AVR", 8, ["PCM", "Dolby TrueHD"]))
    def test_pi4_does_not_claim_atmos_without_direct_player(self, _decode):
        audio = audio_capabilities("Raspberry Pi 4 Model B Rev 1.5")
        profiles = {profile["id"]: profile for profile in audio["profiles"]}
        self.assertFalse(profiles["hdmi_atmos_direct"]["available"])
        self.assertTrue(profiles["analog_stereo"]["available"])

    def test_common_parsed_scancodes(self):
        protocol, scancode = NodeRuntime._parsed_scancode(
            {"protocol": "NEC", "address": "10 00 00 00", "command": "01 00 00 00"}
        )
        self.assertEqual(protocol, "nec")
        self.assertEqual(scancode, 0x1001)

    @patch("node.agent._edid_decode", return_value=(True, "Denon AVR", 8, ["PCM"]))
    @patch("node.agent.shutil.which", return_value="/usr/bin/cec-client")
    @patch("node.agent.subprocess.run")
    def test_hdmi_cec_power_targets_audio_system(self, process, _which, _decode):
        process.return_value = MagicMock(returncode=0, stdout="", stderr="")
        runtime = NodeRuntime({"id": "node", "allowCecControl": True, "cecLogicalAddress": 5})
        runtime.audio["detected"]["hdmiConnected"] = True
        result = runtime.cec_power("on")
        self.assertEqual(result["logicalAddress"], 5)
        self.assertEqual(process.call_args.kwargs["input"], "on 5\n")

    @patch("node.agent.Path.exists", return_value=True)
    @patch("node.agent.run")
    @patch("node.agent.hardware_model", return_value="Raspberry Pi Zero 2 W Rev 1.0")
    def test_soft_sleep_wake_invokes_exact_power_helper(self, _model, command, _exists):
        command.return_value = MagicMock(returncode=0, stdout="", stderr="")
        runtime = NodeRuntime({"id": "node", "allowNodePowerControl": True})
        result = runtime.system_power("wake")
        self.assertEqual(result["state"], "awake")
        command.assert_called_once_with(
            ["sudo", "-n", str(Path("/usr/local/sbin/hearth-node-power")), "wake"],
            timeout=8,
        )

    @patch("node.agent.Path.exists", return_value=True)
    @patch("node.agent.run")
    @patch("node.agent.hardware_model", return_value="Raspberry Pi Zero 2 W Rev 1.0")
    def test_node_power_control_requires_explicit_permission(self, _model, command, _exists):
        runtime = NodeRuntime({"id": "node", "allowNodePowerControl": False})
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            runtime.system_power("wake")
        command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
