"""Uploaded sound clips, scheduled playback, and isolated Snapcast routing."""

from __future__ import annotations

import copy
import errno
import json
import os
import queue
import re
import select
import subprocess
import threading
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable


FORMATS = {".mp3", ".wav", ".flac", ".ogg", ".oga", ".m4a", ".aac", ".opus"}
MAX_UPLOAD = 25 * 1024 * 1024
MAX_SOUNDS = 100
PIPE = Path("/run/hearth-audio/soundboard.pcm")
TIME = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class Soundboard:
    def __init__(self, state_path: Path | None, nodes: Callable[[], list[dict[str, Any]]], rpc: Callable[..., dict[str, Any]], enabled: bool = True):
        self.path = state_path.with_name("soundboard.json") if state_path else None
        self.directory = state_path.parent / "sounds" if state_path else None
        self.nodes = nodes
        self.rpc = rpc
        self.lock = threading.RLock()
        self.jobs: queue.Queue[tuple[list[str], list[str]]] = queue.Queue(maxsize=16)
        self.stop_event = threading.Event()
        self.data: dict[str, Any] = {"sounds": [], "schedules": []}
        self.active: dict[str, Any] | None = None
        self.error = ""
        self.enabled = enabled
        if self.path and self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                self.data = {key: loaded.get(key, []) for key in ("sounds", "schedules")}
            except (OSError, ValueError):
                self.error = "The saved soundboard catalogue could not be read."
        if self.directory:
            self.directory.mkdir(parents=True, exist_ok=True)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {**copy.deepcopy(self.data), "active": copy.deepcopy(self.active), "error": self.error, "enabled": self.enabled}

    def set_enabled(self, enabled: bool) -> None:
        with self.lock:
            self.enabled = enabled
            if not enabled:
                self.stop()

    def require_enabled(self) -> None:
        if not self.enabled:
            raise ValueError("The soundboard is turned off in System settings.")

    def save(self) -> None:
        if not self.path:
            return
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    def upload(self, name: str, content: bytes) -> dict[str, Any]:
        self.require_enabled()
        clean = Path(name.replace("\\", "/")).name.strip()[:120]
        suffix = Path(clean).suffix.lower()
        if not clean or suffix not in FORMATS:
            raise ValueError("Choose an MP3, WAV, FLAC, Ogg, M4A, AAC, or Opus audio file.")
        if not content or len(content) > MAX_UPLOAD:
            raise ValueError("Sound files must be between 1 byte and 25 MB.")
        with self.lock:
            if len(self.data["sounds"]) >= MAX_SOUNDS:
                raise ValueError("The soundboard is full (100 sounds maximum).")
            sound = {"id": uuid.uuid4().hex, "name": clean, "size": len(content)}
            if self.directory:
                target = self.directory / (sound["id"] + suffix)
                target.write_bytes(content)
            self.data["sounds"].append(sound)
            self.save()
            return copy.deepcopy(sound)

    def delete_sound(self, sound_id: str) -> None:
        with self.lock:
            sound = next((item for item in self.data["sounds"] if item["id"] == sound_id), None)
            if sound is None:
                raise KeyError(sound_id)
            if self.active and sound_id in self.active["soundIds"]:
                raise ValueError("Stop this sound before deleting it.")
            self.data["sounds"].remove(sound)
            self.data["schedules"] = [item for item in self.data["schedules"] if sound_id not in item["soundIds"]]
            self.save()
            if self.directory:
                (self.directory / (sound_id + Path(sound["name"]).suffix.lower())).unlink(missing_ok=True)

    def validate_selection(self, sound_ids: list[str], target_ids: list[str]) -> None:
        if not isinstance(sound_ids, list) or not isinstance(target_ids, list):
            raise ValueError("Select sounds and playback locations from the lists.")
        if not sound_ids or not target_ids:
            raise ValueError("Select at least one sound and one playback location.")
        if len(sound_ids) > 20 or len(target_ids) > 20:
            raise ValueError("Too many sounds or playback locations selected.")
        if any(not isinstance(item, str) for item in sound_ids + target_ids):
            raise ValueError("Sound and playback location IDs must be text.")
        if len(set(sound_ids)) != len(sound_ids) or len(set(target_ids)) != len(target_ids):
            raise ValueError("Do not select the same sound or location twice.")
        sounds = {sound["id"] for sound in self.data["sounds"]}
        nodes = {node["id"] for node in self.nodes()}
        if any(sound_id not in sounds for sound_id in sound_ids):
            raise ValueError("A selected sound no longer exists.")
        if any(target_id not in nodes for target_id in target_ids):
            raise ValueError("A selected playback location no longer exists.")

    def enqueue(self, sound_ids: list[str], target_ids: list[str]) -> None:
        with self.lock:
            self.require_enabled()
            self.validate_selection(sound_ids, target_ids)
            try:
                self.jobs.put_nowait((list(sound_ids), list(target_ids)))
            except queue.Full as exc:
                raise ValueError("The soundboard queue is full.") from exc

    def add_schedule(self, values: dict[str, Any]) -> dict[str, Any]:
        self.require_enabled()
        sound_ids = values.get("soundIds", [])
        target_ids = values.get("targetNodeIds", [])
        scheduled_time = str(values.get("time", ""))
        days = values.get("days", [])
        expires_on = str(values.get("expiresOn", ""))
        with self.lock:
            self.validate_selection(sound_ids, target_ids)
            if not TIME.fullmatch(scheduled_time):
                raise ValueError("Choose a valid local time.")
            if not isinstance(days, list) or not days or any(type(day) is not int or day not in range(7) for day in days):
                raise ValueError("Choose at least one day of the week.")
            if expires_on:
                try:
                    date.fromisoformat(expires_on)
                except ValueError as exc:
                    raise ValueError("Choose a valid expiration date.") from exc
            schedule = {
                "id": uuid.uuid4().hex,
                "soundIds": list(sound_ids),
                "targetNodeIds": list(target_ids),
                "time": scheduled_time,
                "days": sorted(set(days)),
                "expiresOn": expires_on,
                "lastRunDate": "",
            }
            self.data["schedules"].append(schedule)
            self.save()
            return copy.deepcopy(schedule)

    def delete_schedule(self, schedule_id: str) -> None:
        with self.lock:
            before = len(self.data["schedules"])
            self.data["schedules"] = [item for item in self.data["schedules"] if item["id"] != schedule_id]
            if len(self.data["schedules"]) == before:
                raise KeyError(schedule_id)
            self.save()

    def tick(self, now: datetime | None = None) -> None:
        now = now or datetime.now()
        today = now.date().isoformat()
        with self.lock:
            before = len(self.data["schedules"])
            self.data["schedules"] = [item for item in self.data["schedules"] if not item.get("expiresOn") or item["expiresOn"] >= today]
            changed = len(self.data["schedules"]) != before
            for item in self.data["schedules"]:
                if (self.enabled and now.weekday() in item["days"] and item["time"] == now.strftime("%H:%M")
                        and item.get("lastRunDate") != today):
                    try:
                        self.enqueue(item["soundIds"], item["targetNodeIds"])
                    except ValueError as exc:
                        self.error = f"Scheduled sound skipped: {exc}"
                    item["lastRunDate"] = today
                    changed = True
            if changed:
                self.save()

    def stop(self) -> None:
        self.stop_event.set()
        while True:
            try:
                self.jobs.get_nowait()
                self.jobs.task_done()
            except queue.Empty:
                break
        with self.lock:
            if self.active is None:
                self.stop_event.clear()

    def _route(self, target_ids: list[str]) -> list[tuple[str, str]]:
        status = self.rpc("Server.GetStatus")
        server = status.get("server", {})
        if "Soundboard" not in {stream.get("id") for stream in server.get("streams", [])}:
            raise ValueError("Snapserver's Soundboard stream is unavailable. Update the Hub audio configuration.")
        from hub.server import snapclient_for_node
        restore: list[tuple[str, str]] = []
        for target_id in target_ids:
            node = next((item for item in self.nodes() if item["id"] == target_id), None)
            client = snapclient_for_node(status, node) if node else None
            if not client or not client.get("connected"):
                raise ValueError(f"Playback location {target_id} is not connected to Snapcast.")
            group = next((item for item in server.get("groups", []) if client in item.get("clients", [])), None)
            if group is None or len(group.get("clients", [])) != 1:
                raise ValueError("Each selected playback location must have its own Snapcast group.")
            restore.append((str(group["id"]), str(group["stream_id"])))
        switched: list[tuple[str, str]] = []
        try:
            for group_id, previous in restore:
                self.rpc("Group.SetStream", {"id": group_id, "stream_id": "Soundboard"})
                switched.append((group_id, previous))
        except Exception:
            self._restore(switched)
            raise
        return restore

    def _restore(self, groups: list[tuple[str, str]]) -> None:
        for group_id, stream_id in groups:
            try:
                self.rpc("Group.SetStream", {"id": group_id, "stream_id": stream_id})
            except Exception as exc:
                with self.lock:
                    self.error = f"Could not restore an audio group: {exc}"

    def _play(self, sound_ids: list[str], target_ids: list[str]) -> None:
        if not self.directory:
            raise ValueError("The soundboard needs a persistent Hub state directory.")
        with self.lock:
            self.require_enabled()
            self.validate_selection(sound_ids, target_ids)
            sounds = [next(item for item in self.data["sounds"] if item["id"] == ident) for ident in sound_ids]
            self.active = {"soundIds": sound_ids, "targetNodeIds": target_ids, "startedAt": time.time()}
            self.error = ""
        groups: list[tuple[str, str]] = []
        keepalive: int | None = None
        try:
            # Keep one FIFO endpoint open before changing groups. Otherwise
            # Snapserver can open, see EOF, and close its reader before FFmpeg
            # has produced its first PCM chunk.
            keepalive = os.open(PIPE, os.O_RDWR | os.O_NONBLOCK)
            groups = self._route(target_ids)
            for sound in sounds:
                if self.stop_event.is_set():
                    break
                target = self.directory / (sound["id"] + Path(sound["name"]).suffix.lower())
                command = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(target), "-vn", "-t", "300", "-ar", "48000", "-ac", "2", "-f", "s16le", "pipe:1"]
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                try:
                    # Snapserver opens a source FIFO asynchronously after the
                    # group switches streams. A first nonblocking open can
                    # legitimately see ENXIO before its reader is attached.
                    deadline = time.monotonic() + 5
                    while True:
                        try:
                            descriptor = os.open(PIPE, os.O_WRONLY | os.O_NONBLOCK)
                            break
                        except OSError as error:
                            if error.errno != errno.ENXIO or time.monotonic() >= deadline:
                                raise
                            time.sleep(0.05)
                    try:
                        while not self.stop_event.is_set():
                            chunk = process.stdout.read(38400)
                            if not chunk:
                                break
                            remaining = memoryview(chunk)
                            while remaining:
                                if self.stop_event.is_set():
                                    break
                                if not select.select([], [descriptor], [], 5)[1]:
                                    raise TimeoutError("Snapserver did not read the Soundboard audio pipe.")
                                try:
                                    remaining = remaining[os.write(descriptor, remaining):]
                                except BlockingIOError:
                                    continue
                    finally:
                        os.close(descriptor)
                except Exception:
                    process.terminate()
                    raise
                finally:
                    if self.stop_event.is_set():
                        process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    if process.stdout:
                        process.stdout.close()
                if process.returncode and not self.stop_event.is_set():
                    raise ValueError(f"Could not decode {sound['name']} with FFmpeg.")
            if not self.stop_event.is_set():
                time.sleep(1.5)  # let Snapclient's one-second buffer play the final samples
        finally:
            self._restore(groups)
            if keepalive is not None:
                os.close(keepalive)
            with self.lock:
                self.active = None
                self.stop_event.clear()

    def worker_forever(self) -> None:
        while True:
            sounds, targets = self.jobs.get()
            try:
                self._play(sounds, targets)
            except Exception as exc:
                with self.lock:
                    self.error = str(exc)
                    self.active = None
                    self.stop_event.clear()
            finally:
                self.jobs.task_done()

    def scheduler_forever(self) -> None:
        while True:
            try:
                self.tick()
            except Exception as exc:
                with self.lock:
                    self.error = f"Scheduler error: {exc}"
            time.sleep(10)

    def start(self) -> None:
        threading.Thread(target=self.worker_forever, name="hearth-soundboard", daemon=True).start()
        threading.Thread(target=self.scheduler_forever, name="hearth-soundboard-scheduler", daemon=True).start()
