import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from node.display import KDSETMODE, KD_GRAPHICS, HubClient, NativeDisplay, effective_position, format_duration


class NativeDisplayTests(unittest.TestCase):
    def test_formats_track_durations(self):
        self.assertEqual(format_duration(0), "0:00")
        self.assertEqual(format_duration(65.9), "1:05")
        self.assertEqual(format_duration(None), "0:00")

    def test_advances_and_caps_live_position(self):
        playback = {
            "state": "playing",
            "positionSeconds": 10,
            "positionUpdatedAt": 100,
            "durationSeconds": 15,
        }
        self.assertEqual(effective_position(playback, now=102), 12)
        self.assertEqual(effective_position(playback, now=120), 15)

    def test_paused_position_does_not_advance(self):
        playback = {
            "state": "paused",
            "positionSeconds": 10,
            "positionUpdatedAt": 100,
            "durationSeconds": 20,
        }
        self.assertEqual(effective_position(playback, now=150), 10)

    def test_missing_artwork_does_not_make_hub_unavailable(self):
        client = HubClient("http://hub", "node-2")
        state = {"system": {"playback": {"artworkRevision": 7}}}

        def request(path, data=None, timeout=3.0):
            if path == "/api/state":
                return __import__("json").dumps(state).encode()
            raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)

        with patch.object(client, "_request", side_effect=request):
            client.poll()

        current, error, revision, artwork = client.snapshot()
        self.assertEqual(current, state)
        self.assertEqual(error, "")
        self.assertEqual(revision, 7)
        self.assertIsNone(artwork)

    def test_hub_errors_use_generic_display_message(self):
        client = HubClient("http://hub", "node-2")
        with patch.object(client, "_request", side_effect=urllib.error.URLError("private details")):
            client.poll()

        current, error, _, _ = client.snapshot()
        self.assertIsNone(current)
        self.assertEqual(error, "Hub Not Connected")

    def test_console_graphics_constants_match_linux_kd_api(self):
        self.assertEqual(KDSETMODE, 0x4B3A)
        self.assertEqual(KD_GRAPHICS, 1)

    def test_fbdev_artwork_does_not_require_sdl_video_conversion(self):
        display = object.__new__(NativeDisplay)
        display.backend = "fbdev"
        display.artwork_revision = -1
        display.artwork = None
        display.artwork_scaled = None
        display.height = 900
        display.width = 1600
        display.screen = MagicMock()
        decoded = MagicMock()
        decoded.convert.side_effect = AssertionError("fbdev must not require an SDL display")
        display.pg = MagicMock()
        display.pg.image.load.return_value = decoded
        scaled = MagicMock()
        display.pg.transform.smoothscale.return_value = scaled

        display.update_artwork(3, b"jpeg bytes")

        decoded.convert.assert_not_called()
        display.pg.transform.smoothscale.assert_called_once_with(decoded, (468, 468))
        self.assertIs(display.artwork_scaled, scaled)


if __name__ == "__main__":
    unittest.main()
