import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

from hub.soundboard import Soundboard


class SoundboardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.nodes = [{"id": "hearth-hub", "local": True, "snapcastClientId": "hearth-hub"}]
        self.rpc = Mock()
        self.board = Soundboard(Path(self.temporary.name) / "state.json", lambda: self.nodes, self.rpc)

    def test_upload_is_persisted_and_blocks_bad_extensions(self):
        sound = self.board.upload("folder\\bell.mp3", b"audio")
        self.assertEqual(sound["name"], "bell.mp3")
        self.assertTrue((self.board.directory / (sound["id"] + ".mp3")).exists())
        restored = Soundboard(Path(self.temporary.name) / "state.json", lambda: self.nodes, self.rpc)
        self.assertEqual(restored.snapshot()["sounds"], [sound])
        with self.assertRaisesRegex(ValueError, "Choose an MP3"):
            self.board.upload("script.py", b"not audio")

    def test_disabling_soundboard_keeps_files_but_blocks_play_and_schedule(self):
        sound = self.board.upload("bell.mp3", b"audio")
        self.board.set_enabled(False)
        self.assertFalse(self.board.snapshot()["enabled"])
        self.assertEqual(self.board.snapshot()["sounds"], [sound])
        with self.assertRaisesRegex(ValueError, "turned off"):
            self.board.enqueue([sound["id"]], ["hearth-hub"])
        with self.assertRaisesRegex(ValueError, "turned off"):
            self.board.add_schedule({"soundIds": [sound["id"]], "targetNodeIds": ["hearth-hub"], "time": "08:00", "days": [0]})

    def test_weekly_schedule_fires_once_and_expires_after_date(self):
        sound = self.board.upload("bell.wav", b"audio")
        schedule = self.board.add_schedule({
            "soundIds": [sound["id"]], "targetNodeIds": ["hearth-hub"],
            "time": "08:30", "days": [0], "expiresOn": "2026-10-05",
        })
        monday = datetime(2026, 10, 5, 8, 30)
        self.board.tick(monday)
        self.board.tick(monday)
        self.assertEqual(self.board.jobs.qsize(), 1)
        self.assertEqual(self.board.snapshot()["schedules"][0]["lastRunDate"], "2026-10-05")
        self.board.tick(datetime(2026, 10, 6, 0, 1))
        self.assertEqual(self.board.snapshot()["schedules"], [])
        self.assertEqual(schedule["id"] in self.board.path.read_text(), False)

    def test_routes_only_single_client_group_and_restores_previous_stream(self):
        self.rpc.return_value = {"server": {
            "streams": [{"id": "AirPlay"}, {"id": "Soundboard"}],
            "groups": [{"id": "group-hub", "stream_id": "AirPlay", "clients": [{
                "id": "hearth-hub", "connected": True, "host": {"name": "hearth-hub"},
            }]}],
        }}
        restore = self.board._route(["hearth-hub"])
        self.assertEqual(restore, [("group-hub", "AirPlay")])
        self.rpc.assert_any_call("Group.SetStream", {"id": "group-hub", "stream_id": "Soundboard"})
        self.board._restore(restore)
        self.rpc.assert_any_call("Group.SetStream", {"id": "group-hub", "stream_id": "AirPlay"})

    def test_rejects_shared_group_to_avoid_switching_other_rooms(self):
        self.rpc.return_value = {"server": {
            "streams": [{"id": "Soundboard"}],
            "groups": [{"id": "shared", "stream_id": "AirPlay", "clients": [
                {"id": "hearth-hub", "connected": True, "host": {"name": "hearth-hub"}},
                {"id": "another", "connected": True, "host": {"name": "another"}},
            ]}],
        }}
        with self.assertRaisesRegex(ValueError, "own Snapcast group"):
            self.board._route(["hearth-hub"])


if __name__ == "__main__":
    unittest.main()
