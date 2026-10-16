import re
import runpy
import unittest
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "hub" / "static"


class IdParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if "id" in values:
            self.ids.append(values["id"])


class StaticUiTests(unittest.TestCase):
    def test_node_2_hdmi_mode_preserves_existing_cmdline_and_user_override(self):
        script = ROOT.parents[1] / "deploy" / "configure-node-2-hdmi.py"
        if not script.exists():
            self.skipTest("Legacy local Node 2 display override is not in the public release")
        configure = runpy.run_path(str(script))["update_cmdline"]
        self.assertEqual(configure("console=tty1 rootwait\n"), "console=tty1 rootwait video=HDMI-A-1:1920x1080@60\n")
        self.assertEqual(configure("console=tty1 video=HDMI-A-1:1280x720@60\n"), "console=tty1 video=HDMI-A-1:1280x720@60\n")

    def test_html_ids_are_unique(self):
        parser = IdParser()
        parser.feed((ROOT / "index.html").read_text(encoding="utf-8"))
        self.assertEqual(len(parser.ids), len(set(parser.ids)))

    def test_javascript_id_selectors_exist(self):
        parser = IdParser()
        parser.feed((ROOT / "index.html").read_text(encoding="utf-8"))
        known = set(parser.ids)
        javascript = (ROOT / "app.js").read_text(encoding="utf-8")
        referenced = set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"', javascript))
        self.assertEqual(referenced - known, set())

    def test_touchscreen_breakpoint_is_present(self):
        css = (ROOT / "styles.css").read_text(encoding="utf-8")
        self.assertIn("@media (max-height: 550px)", css)
        self.assertIn("@media (max-width: 700px)", css)

    def test_global_surround_mode_selector_is_wired(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="playback-mode"', html)
        self.assertIn('value="surround_51"', html)
        self.assertIn('value="surround_71"', html)
        self.assertIn('JSON.stringify({ playbackMode: control.value })', javascript)

    def test_requested_player_controls_and_dimming_are_present(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="open-player-screensaver"', html)
        self.assertNotIn("playing-bars", html)
        self.assertIn("30000", javascript)
        self.assertIn('get("display") === "node"', javascript)

    def test_display_preferences_are_client_local_and_support_quiet_hours(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="quiet-hours-enabled"', html)
        self.assertIn('id="display-off"', html)
        self.assertIn("CLIENT_SETTINGS_KEY", javascript)
        self.assertIn("localStorage.setItem", javascript)
        self.assertIn("isQuietHours()", javascript)
        self.assertIn("setHardwareDisplayPower(\"off\")", javascript)
        self.assertNotIn('JSON.stringify({ screensaverSeconds: seconds })', javascript)

    def test_node_output_gain_segment_is_wired(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="output-gain"', html)
        self.assertIn('data-value="-6"', html)
        self.assertIn('data-value="6"', html)
        self.assertIn('data-value="9"', html)
        self.assertIn('data-value="12"', html)
        self.assertIn("outputGainDb", javascript)

    def test_offline_touch_and_screensaver_ui(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "styles.css").read_text(encoding="utf-8")
        self.assertNotIn("Tap anywhere to return", html)
        self.assertIn('"No Output"', javascript)
        self.assertIn("touch-action: pan-y", css)
        self.assertIn("display/heartbeat", javascript)
        self.assertIn("volumeDragNodeId", javascript)
        self.assertIn(".local-node .power-button", css)
        self.assertNotIn("CONTROL FROM AIRPLAY SENDER", html)

    def test_temporary_browser_node_ui_and_snapweb_are_wired(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "app.js").read_text(encoding="utf-8")
        deploy = ROOT.parents[1] / "deploy"
        snapserver = (deploy / "snapserver.conf").read_text(encoding="utf-8")
        snapweb_installer = (deploy / "install-snapweb.sh").read_text(encoding="utf-8")
        self.assertIn("Temporary web node", html)
        self.assertIn('id="node-type-modal"', html)
        self.assertIn('id="add-permanent-node-choice"', html)
        self.assertIn('id="add-temporary-node-choice"', html)
        self.assertIn('placeholder="Type temporary node name"', html)
        self.assertIn("owned-browser-node", javascript)
        self.assertIn("remove-temp-button", javascript)
        self.assertNotIn("▣", html)
        self.assertIn("audioReady", javascript)
        self.assertIn("doc_root = /usr/share/snapserver/snapweb/", snapserver)
        self.assertIn('VERSION="0.9.3"', snapweb_installer)
        self.assertIn("cc258df98b8c474ea19048bc84c0c2e2c5fbc3b0856aef03a838f3a4489fa4c7", snapweb_installer)

    def test_snapclient_output_uses_pcm_conversion_and_software_volume(self):
        deploy = ROOT.parents[1] / "deploy"
        helper = (deploy / "hearth-snapclient-output").read_text(encoding="utf-8")
        self.assertIn('physical_soundcard = f"plughw:CARD={card},DEV={device}"', helper)
        self.assertIn('cleaned.extend(["--mixer", "software", "--soundcard", soundcard])', helper)
        self.assertIn('{"hdmi_51_pcm": 6, "hdmi_71_pcm": 8}', helper)
        self.assertIn('BOOST_DB = 12.0', helper)
        self.assertIn('type softvol', helper)
        self.assertIn('update_config(BOOST_PCM)', helper)

        node_updater_path = deploy / "update-node.ps1"
        node_updater = (node_updater_path if node_updater_path.exists() else deploy / "install-node.sh").read_text(encoding="utf-8")
        self.assertIn('Saved output \'$OUTPUT_PROFILE\' is currently unavailable', node_updater)
        self.assertIn('hearth-snapclient-output auto', node_updater)
        self.assertIn('hearth-snapclient-output null', node_updater)

        mode_helper = (deploy / "hearth-playback-mode").read_text(encoding="utf-8")
        self.assertIn('MODES = {"stereo": 2, "surround_51": 6, "surround_71": 8}', mode_helper)
        self.assertIn("doc_root = /usr/share/snapserver/snapweb/", mode_helper)
        self.assertIn("was rolled back", mode_helper)

    def test_node_display_uses_native_sdl_without_chromium(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        display_launcher = (ROOT.parents[1] / "deploy" / "hearth-node-display").read_text(encoding="utf-8")
        native_display = (ROOT.parents[1] / "node" / "display.py").read_text(encoding="utf-8")
        service = (ROOT.parents[1] / "deploy" / "hearth-node-display.service").read_text(encoding="utf-8")
        prepare = (ROOT.parents[1] / "deploy" / "hearth-node-display-prepare").read_text(encoding="utf-8")
        self.assertIn("SDL_VIDEODRIVER=x11", display_launcher)
        self.assertIn("SDL_VIDEODRIVER=wayland", display_launcher)
        self.assertIn("SDL_VIDEODRIVER=kmsdrm", display_launcher)
        self.assertNotIn("chromium", display_launcher.lower())
        self.assertIn("pygame.mouse.set_visible(False)", native_display)
        self.assertIn("draw_startup_test", native_display)
        self.assertIn("frame = self.screen", native_display)
        self.assertIn('self.framebuffer_path.open("r+b", buffering=0)', native_display)
        self.assertIn("fcntl.ioctl(0, KDSETMODE, KD_GRAPHICS)", native_display)
        self.assertIn("bits_per_pixel not in {16, 32}", native_display)
        self.assertNotIn("pygame.DOUBLEBUF", native_display)
        self.assertIn("display-heartbeat", native_display)
        self.assertIn("/api/artwork", native_display)
        self.assertIn('HEARTH_DISPLAY_LAYOUT', native_display)
        self.assertIn('self.framebuffer_path', native_display)
        self.assertIn('if self.compact:', native_display)
        self.assertIn("display/heartbeat", native_display)
        self.assertIn("WantedBy=multi-user.target", service)
        self.assertIn("Conflicts=getty@tty1.service", service)
        self.assertIn("TTYPath=/dev/tty1", service)
        self.assertIn("StandardInput=tty-force", service)
        self.assertIn("PAMName=login", service)
        self.assertIn("Environment=HEARTH_DISPLAY_BACKEND=fbdev", service)
        self.assertIn("Environment=SDL_VIDEODRIVER=dummy", service)
        self.assertIn("Environment=XDG_RUNTIME_DIR=/run/hearth-node-display", service)
        self.assertIn("RuntimeDirectory=hearth-node-display", service)
        self.assertIn("hearth-node-display-prepare", service)
        self.assertIn("/sys/class/graphics/fb*/blank", prepare)
        self.assertIn("printf '0'", prepare)
        self.assertIn("printf '\\033c'", prepare)
        self.assertIn("printf '\\033[?25l'", prepare)
        self.assertIn("setterm --cursor off", prepare)
        self.assertIn('document.documentElement.classList.add("display-only", "cursor-hidden")', html)

        deploy = ROOT.parents[1] / "deploy"
        provision = (deploy / "provision-node.py").read_text(encoding="utf-8")
        spotpear = (deploy / "install-spotpear-display.sh").read_text(encoding="utf-8")
        launcher = (deploy / "hearth-node-display").read_text(encoding="utf-8")
        hdmi_launcher = (deploy / "hearth-node-hdmi-display").read_text(encoding="utf-8")
        hdmi_prepare = (deploy / "hearth-node-hdmi-display-prepare").read_text(encoding="utf-8")
        hdmi_service = (deploy / "hearth-node-hdmi-display.service").read_text(encoding="utf-8")
        self.assertIn('"type": "spotpear_st7789"', provision)
        self.assertIn('"secondaryHdmi": True', provision)
        self.assertIn('button_gpio is not None', provision)
        self.assertIn("1921351a6e55d30f10898e52a0e081fde2725af9b6177f06b05c6cca6fe8a366", spotpear)
        self.assertIn("dtoverlay={overlay}", spotpear)
        self.assertIn("Waiting for the SpotPear framebuffer", launcher)
        self.assertIn("HEARTH_FRAMEBUFFER=/dev/fb0", hdmi_launcher)
        self.assertIn("HEARTH_DISPLAY_LAYOUT=full", hdmi_launcher)
        self.assertIn("Waiting for an HDMI display and /dev/fb0", hdmi_launcher)
        self.assertIn("hearth-node-display-prepare", hdmi_prepare)
        self.assertIn("Environment=HEARTH_FRAMEBUFFER=/dev/fb0", hdmi_service)
        self.assertIn("StandardInput=null", hdmi_service)

    def test_power_controls_separate_receiver_transport_from_pi_state(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "app.js").read_text(encoding="utf-8")
        helper = (ROOT.parents[1] / "deploy" / "hearth-node-power").read_text(encoding="utf-8")
        self.assertIn('data-tab="power"', html)
        self.assertIn('data-value="hdmi_cec"', html)
        self.assertIn('value="soft_sleep"', html)
        self.assertIn('value="halt"', html)
        self.assertIn('GPIO3 / physical pin 5', javascript)
        self.assertIn("systemctl stop --no-block hearth-node-hdmi-display.service", helper)
        self.assertIn("is-enabled hearth-node-hdmi-display.service", helper)

    def test_hub_and_drawer_support_touch_scrolling(self):
        css = (ROOT / "styles.css").read_text(encoding="utf-8")
        self.assertRegex(css, r"\.main \{[^}]*overflow-y: auto")
        self.assertRegex(css, r"\.main \{[^}]*touch-action: pan-y")
        self.assertRegex(css, r"\.drawer-content \{[^}]*overflow-y: scroll")
        self.assertRegex(css, r"\.drawer-content \{[^}]*touch-action: pan-y")

    def test_labwc_cursor_configuration_is_valid_and_idempotent(self):
        script = ROOT.parents[1] / "deploy" / "configure-node-cursor.py"
        if not script.exists():
            self.skipTest("Legacy local desktop cursor setup is not in the public release")
        update = runpy.run_path(str(script))["update"]
        original = '<?xml version="1.0"?><openbox_config><keyboard /></openbox_config>'
        configured = update(original)
        root = ET.fromstring(configured)
        self.assertIn("HEARTH_NODE_HIDE_CURSOR", configured)
        self.assertIsNotNone(root.find("./keyboard/keybind"))
        self.assertEqual(update(configured), configured)


if __name__ == "__main__":
    unittest.main()
