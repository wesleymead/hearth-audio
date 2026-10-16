"""Read Shairport Sync's XML metadata pipe and update the hub state."""

from __future__ import annotations

import base64
import binascii
import re
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Protocol


ITEM_PATTERN = re.compile(br"<item>.*?</item>", re.DOTALL)
CORE_FIELDS = {"minm": "title", "asar": "artist", "asal": "album"}


class MetadataStore(Protocol):
    def apply_playback(self, changes: dict[str, object]) -> None: ...

    def set_artwork(self, content: bytes, media_type: str) -> None: ...


def _fourcc(value: str) -> str:
    try:
        return bytes.fromhex(value.strip()).decode("ascii")
    except (ValueError, UnicodeDecodeError):
        return value.strip()


def _artwork_type(content: bytes) -> str:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return "application/octet-stream"


class ShairportMetadataReader:
    """Incremental parser for Shairport Sync metadata `<item>` records."""

    def __init__(self, store: MetadataStore, pipe_path: Path):
        self.store = store
        self.pipe_path = pipe_path
        self.buffer = b""
        self.pending: dict[str, object] = {}

    def feed(self, content: bytes) -> None:
        self.buffer += content
        while match := ITEM_PATTERN.search(self.buffer):
            self.buffer = self.buffer[match.end() :]
            self._parse_item(match.group())
        if len(self.buffer) > 4_000_000:
            self.buffer = self.buffer[-4096:]

    def _parse_item(self, raw: bytes) -> None:
        try:
            item = ET.fromstring(raw)
        except ET.ParseError:
            return
        kind = _fourcc(item.findtext("type", ""))
        code = _fourcc(item.findtext("code", ""))
        data_node = item.find("data")
        payload = b""
        if data_node is not None and data_node.text:
            try:
                payload = base64.b64decode(data_node.text, validate=False)
            except (ValueError, binascii.Error):
                return
        self.handle(kind, code, payload)

    def handle(self, kind: str, code: str, payload: bytes = b"") -> None:
        if kind == "core" and code in CORE_FIELDS:
            self.pending[CORE_FIELDS[code]] = payload.decode("utf-8", errors="replace")
            return
        if kind != "ssnc":
            return

        if code == "mdst":
            self.pending = {}
        elif code == "mden":
            if self.pending:
                self.store.apply_playback(self.pending)
        elif code == "PICT" and payload:
            self.store.set_artwork(payload, _artwork_type(payload))
        elif code in {"pbeg", "prsm"}:
            self.store.apply_playback({"state": "playing", "source": "AirPlay 2", "live": True})
        elif code in {"pend", "disc"}:
            self.pending = {}
            self.store.apply_playback(
                {
                    "state": "stopped",
                    "title": "",
                    "artist": "",
                    "album": "",
                    "client": "",
                    "positionSeconds": 0,
                    "durationSeconds": 0,
                    "live": True,
                    "artworkRevision": 0,
                }
            )
        elif code == "pfls":
            self.store.apply_playback({"state": "paused", "positionUpdatedAt": time.time(), "live": True})
        elif code in {"snam", "clip"}:
            self.store.apply_playback({"client": payload.decode("utf-8", errors="replace"), "live": True})
        elif code == "prgr":
            self._progress(payload)

    def _progress(self, payload: bytes) -> None:
        try:
            start_text, current_text, end_text = payload.decode("ascii").split("/")
            start, current, end = int(start_text), int(current_text), int(end_text)
        except (ValueError, UnicodeDecodeError):
            return
        # AirPlay RTP timestamps wrap at 2^32. Shairport's metadata reader
        # documents these progress values as 44,100 frames per second.
        position = ((current - start) & 0xFFFFFFFF) / 44_100
        duration = ((end - start) & 0xFFFFFFFF) / 44_100
        self.store.apply_playback(
            {
                "positionSeconds": min(position, duration),
                "durationSeconds": duration,
                "positionUpdatedAt": time.time(),
                "live": True,
            }
        )

    def run(self) -> None:
        """Wait for the FIFO and reconnect whenever its writer restarts."""
        while True:
            if not self.pipe_path.exists():
                time.sleep(1)
                continue
            try:
                with self.pipe_path.open("rb", buffering=0) as pipe:
                    while chunk := pipe.read(65_536):
                        self.feed(chunk)
            except OSError:
                time.sleep(1)

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run, name="shairport-metadata", daemon=True)
        thread.start()
        return thread
