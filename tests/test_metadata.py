import base64
import unittest
from pathlib import Path

from hub.metadata import ShairportMetadataReader


class FakeStore:
    def __init__(self):
        self.playback = {}
        self.artwork = None

    def apply_playback(self, changes):
        self.playback.update(changes)

    def set_artwork(self, content, media_type):
        self.artwork = (content, media_type)


def item(kind, code, payload=b""):
    encoded = base64.b64encode(payload).decode()
    return (
        f"<item><type>{kind.encode().hex()}</type><code>{code.encode().hex()}</code>"
        f"<length>{len(payload)}</length><data encoding=\"base64\">{encoded}</data></item>"
    ).encode()


class MetadataReaderTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.reader = ShairportMetadataReader(self.store, Path("unused"))

    def test_reads_track_fields_across_partial_chunks(self):
        payload = item("ssnc", "mdst") + item("core", "minm", b"Test Song") + item("ssnc", "mden")
        self.reader.feed(payload[:37])
        self.reader.feed(payload[37:])
        self.assertEqual(self.store.playback["title"], "Test Song")

    def test_reads_progress_and_play_state(self):
        self.reader.feed(item("ssnc", "pbeg") + item("ssnc", "prgr", b"100/44200/88300"))
        self.assertEqual(self.store.playback["state"], "playing")
        self.assertAlmostEqual(self.store.playback["positionSeconds"], 1.0)
        self.assertAlmostEqual(self.store.playback["durationSeconds"], 2.0)

    def test_detects_png_artwork(self):
        content = b"\x89PNG\r\n\x1a\nart"
        self.reader.feed(item("ssnc", "PICT", content))
        self.assertEqual(self.store.artwork, (content, "image/png"))


if __name__ == "__main__":
    unittest.main()
