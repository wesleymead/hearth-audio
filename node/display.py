#!/usr/bin/python3
"""Lightweight native now-playing display for Hearth playback nodes."""

from __future__ import annotations

import argparse
import io
import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import fcntl  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - permits Windows-side tests
    fcntl = None  # type: ignore


BACKGROUND = (7, 11, 16)
PANEL = (17, 24, 31)
TEXT = (244, 241, 234)
MUTED = (156, 163, 170)
ACCENT = (240, 171, 72)
TRACK = (74, 86, 98)
FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
KDSETMODE = 0x4B3A
KD_TEXT = 0x00
KD_GRAPHICS = 0x01


def format_duration(seconds: object) -> str:
    try:
        whole = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        whole = 0
    return f"{whole // 60}:{whole % 60:02d}"


def effective_position(playback: dict[str, Any], now: float | None = None) -> float:
    current = float(playback.get("positionSeconds") or 0)
    if playback.get("state") == "playing" and playback.get("positionUpdatedAt"):
        current += max(0.0, (time.time() if now is None else now) - float(playback["positionUpdatedAt"]))
    duration = float(playback.get("durationSeconds") or 0)
    return min(current, duration) if duration > 0 else current


def load_config(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Node configuration must contain a JSON object.")
    return value


class HubClient:
    def __init__(self, hub_url: str, node_id: str):
        self.hub_url = hub_url.rstrip("/")
        self.node_id = node_id
        self.state: dict[str, Any] | None = None
        self.error = "Hub Not Connected"
        self.artwork_revision = 0
        self.artwork_bytes: bytes | None = None
        self.lock = threading.Lock()

    def _request(self, path: str, data: bytes | None = None, timeout: float = 3.0) -> bytes:
        request = urllib.request.Request(
            self.hub_url + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST" if data is not None else "GET",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()

    def poll(self) -> None:
        try:
            state = json.loads(self._request("/api/state"))
            if not isinstance(state, dict):
                raise ValueError("Hub returned an invalid state document")
        except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as error:
            with self.lock:
                self.state = None
                self.error = "Hub Not Connected"
            print(f"Hearth display could not read Hub state: {error}", flush=True)
            return

        # Artwork is optional. The Hub can retain an artwork revision in its
        # persisted playback state even though the image bytes were lost in a
        # restart. A missing image must not invalidate the valid Hub state.
        playback = state.get("system", {}).get("playback", {})
        revision = int(playback.get("artworkRevision") or 0)
        with self.lock:
            previous_revision = self.artwork_revision
        artwork: bytes | None = None
        artwork_resolved = revision == 0
        if revision and revision != previous_revision:
            try:
                artwork = self._request(f"/api/artwork?v={revision}")
                artwork_resolved = True
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    artwork_resolved = True
                else:
                    print(f"Hearth display could not load artwork: {error}", flush=True)
            except (OSError, urllib.error.URLError) as error:
                print(f"Hearth display could not load artwork: {error}", flush=True)
        with self.lock:
            self.state = state
            self.error = ""
            if revision == 0:
                self.artwork_revision = 0
                self.artwork_bytes = None
            elif revision != self.artwork_revision and artwork_resolved:
                self.artwork_revision = revision
                self.artwork_bytes = artwork

    def heartbeat(self) -> None:
        try:
            self._request(f"/api/nodes/{self.node_id}/display/heartbeat", b"{}", timeout=2.0)
        except (OSError, urllib.error.URLError):
            pass

    def snapshot(self) -> tuple[dict[str, Any] | None, str, int, bytes | None]:
        with self.lock:
            return self.state, self.error, self.artwork_revision, self.artwork_bytes

    def poll_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.poll()
            stop.wait(2.0)

    def heartbeat_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.heartbeat()
            stop.wait(5.0)


class NativeDisplay:
    def __init__(self, pygame: Any):
        self.pg = pygame
        self.backend = os.environ.get("HEARTH_DISPLAY_BACKEND", "kmsdrm").lower()
        self.framebuffer_path = Path(os.environ.get("HEARTH_FRAMEBUFFER", "/dev/fb0"))
        self.framebuffer: Any | None = None
        self.framebuffer_stride = 0
        self.console_graphics = False
        if self.backend == "fbdev":
            # Direct framebuffer rendering needs surfaces, drawing, images,
            # and fonts only.  Avoid pygame.init(): on this Zero 2 W its SDL
            # video/event initialization creates a cursor plane and then
            # stalls even when the dummy driver is selected.
            pygame.font.init()
            # Escape sequences can hide the text caret, but fbcon may restore
            # it after PAM/systemd finishes attaching the service to tty1.
            # KD_GRAPHICS disables all console drawing while Hearth owns the
            # framebuffer, including the independently blinking caret.
            try:
                if fcntl is None:
                    raise OSError("Linux console control is unavailable")
                fcntl.ioctl(0, KDSETMODE, KD_GRAPHICS)
                self.console_graphics = True
                print("Hearth display switched tty1 to graphics mode", flush=True)
            except OSError as error:
                print(f"Hearth display could not switch tty1 to graphics mode: {error}", flush=True)
            display_size, bits_per_pixel, self.framebuffer_stride = self.framebuffer_geometry(self.framebuffer_path)
            masks = (
                (0xF800, 0x07E0, 0x001F, 0)
                if bits_per_pixel == 16
                else (0x00FF0000, 0x0000FF00, 0x000000FF, 0)
            )
            self.screen = pygame.Surface(display_size, depth=bits_per_pixel, masks=masks)
            self.framebuffer = self.framebuffer_path.open("r+b", buffering=0)
        else:
            pygame.init()
            pygame.display.set_caption("Hearth Audio")
            info = pygame.display.Info()
            display_size = (max(640, int(info.current_w or 0)), max(480, int(info.current_h or 0)))
            self.screen = pygame.display.set_mode(display_size, pygame.FULLSCREEN)
            pygame.mouse.set_visible(False)
        print(f"Hearth display mode request: {display_size[0]}x{display_size[1]}", flush=True)
        self.width, self.height = self.screen.get_size()
        self.compact = os.environ.get("HEARTH_DISPLAY_LAYOUT", "").lower() == "compact" or max(self.width, self.height) <= 320
        self.screen.fill(PANEL)
        self.present()
        self.scale = min(self.width / 1280, self.height / 720)
        self.background = self.screen.copy()
        self.draw_background(self.background)
        self.fonts: dict[tuple[int, bool], Any] = {}
        self.artwork: Any | None = None
        self.artwork_scaled: Any | None = None
        self.artwork_revision = -1
        self.last_scene = ""
        self.previous_frame: Any | None = None
        self.transition_started = 0.0
        self.started = time.monotonic()
        self.startup_until = self.started + 8.0
        self.first_frame_logged = False
        self.normal_frame_saved = False
        print(f"Hearth native display: {self.width}x{self.height}, backend {self.backend}", flush=True)

    @staticmethod
    def framebuffer_geometry(framebuffer: Path = Path("/dev/fb0")) -> tuple[tuple[int, int], int, int]:
        graphics = Path("/sys/class/graphics") / framebuffer.name
        try:
            width_text, height_text = (graphics / "virtual_size").read_text(encoding="ascii").strip().split(",", 1)
            size = (int(width_text), int(height_text))
            bits_per_pixel = int((graphics / "bits_per_pixel").read_text(encoding="ascii").strip())
            stride = int((graphics / "stride").read_text(encoding="ascii").strip())
        except (OSError, ValueError) as error:
            raise RuntimeError(f"Could not read Linux framebuffer geometry: {error}") from error
        if min(size) <= 0 or bits_per_pixel not in {16, 32}:
            raise RuntimeError(f"Unsupported framebuffer format: {size[0]}x{size[1]} at {bits_per_pixel} bpp")
        return size, bits_per_pixel, stride

    def present(self) -> None:
        if self.backend != "fbdev":
            self.pg.display.flip()
            return
        pixels = self.screen.get_buffer().raw
        pitch = self.screen.get_pitch()
        if pitch == self.framebuffer_stride:
            output = pixels
        else:
            output = b"".join(
                pixels[row * pitch:row * pitch + min(pitch, self.framebuffer_stride)].ljust(self.framebuffer_stride, b"\0")
                for row in range(self.height)
            )
        self.framebuffer.seek(0)
        self.framebuffer.write(output)

    def close(self) -> None:
        if self.framebuffer is not None:
            self.framebuffer.close()
            self.framebuffer = None
        if self.console_graphics:
            try:
                if fcntl is not None:
                    fcntl.ioctl(0, KDSETMODE, KD_TEXT)
            except OSError:
                pass
            self.console_graphics = False

    def font(self, size: int, bold: bool = False) -> Any:
        scaled = max(9, size) if self.compact else max(12, round(size * self.scale))
        key = (scaled, bold)
        if key not in self.fonts:
            preferred = FONT_BOLD if bold else FONT_REGULAR
            self.fonts[key] = self.pg.font.Font(preferred if Path(preferred).exists() else None, scaled)
        return self.fonts[key]

    def text(self, surface: Any, value: str, size: int, color: tuple[int, int, int], position: tuple[int, int], bold: bool = False) -> Any:
        rendered = self.font(size, bold).render(value, True, color)
        surface.blit(rendered, position)
        return rendered.get_rect(topleft=position)

    def centered_text(self, surface: Any, value: str, size: int, color: tuple[int, int, int], y: int, bold: bool = False) -> None:
        rendered = self.font(size, bold).render(value, True, color)
        surface.blit(rendered, ((self.width - rendered.get_width()) // 2, y))

    def wrapped_lines(self, value: str, size: int, max_width: int, max_lines: int = 3) -> list[str]:
        words = (value or "AirPlay audio").split()
        lines: list[str] = []
        current = ""
        font = self.font(size, True)
        while words and len(lines) < max_lines:
            word = words.pop(0)
            candidate = f"{current} {word}".strip()
            if current and font.size(candidate)[0] > max_width:
                lines.append(current)
                current = word
            else:
                current = candidate
        if words:
            current = f"{current} {' '.join(words)}".strip()
        if current and len(lines) < max_lines:
            while font.size(current)[0] > max_width and len(current) > 4:
                current = current[:-2].rstrip(" …") + "…"
            lines.append(current)
        return lines

    def update_artwork(self, revision: int, content: bytes | None) -> None:
        if revision == self.artwork_revision:
            return
        self.artwork_revision = revision
        self.artwork = None
        self.artwork_scaled = None
        if content:
            try:
                self.artwork = self.pg.image.load(io.BytesIO(content))
                # Surface.convert() requires an initialized SDL video mode.
                # The direct framebuffer backend deliberately initializes only
                # pygame.font, so retain the decoded 24/32-bit image there;
                # Surface.blit performs the RGB565 conversion correctly when
                # the completed frame is composed.
                if self.backend != "fbdev":
                    self.artwork = self.artwork.convert(self.screen)
                art_size = min(self.width - 88, 140) if getattr(self, "compact", False) else min(int(self.height * .52), int(self.width * .34))
                self.artwork_scaled = self.pg.transform.smoothscale(self.artwork, (art_size, art_size))
            except self.pg.error as error:
                print(f"Could not decode album artwork: {error}", flush=True)

    def draw_background(self, frame: Any) -> None:
        frame.fill(BACKGROUND)
        haze = self.pg.Surface((self.width, self.height), self.pg.SRCALPHA)
        self.pg.draw.circle(haze, (37, 64, 78, 42), (int(self.width * .82), int(self.height * .22)), int(self.height * .55))
        self.pg.draw.circle(haze, (112, 68, 24, 32), (int(self.width * .18), int(self.height * .88)), int(self.height * .48))
        frame.blit(haze, (0, 0))

    def draw_idle(self, frame: Any) -> None:
        if self.compact:
            now = datetime.now()
            self.centered_text(frame, "HEARTH AUDIO", 13, ACCENT, 35, True)
            self.centered_text(frame, now.strftime("%I:%M %p").lstrip("0"), 42, TEXT, 76, True)
            self.centered_text(frame, now.strftime("%A"), 17, TEXT, 139, True)
            self.centered_text(frame, now.strftime("%B %d, %Y").replace(" 0", " "), 14, MUTED, 168)
            return
        now = datetime.now()
        time_text = now.strftime("%I:%M %p").lstrip("0")
        date_text = now.strftime("%A, %B %d, %Y").replace(" 0", " ")
        time_font = self.font(136, True)
        date_font = self.font(30)
        total = time_font.get_height() + int(22 * self.scale) + date_font.get_height()
        top = (self.height - total) // 2
        self.centered_text(frame, time_text, 136, TEXT, top, True)
        self.centered_text(frame, date_text, 30, MUTED, top + time_font.get_height() + int(22 * self.scale))

    def draw_startup_test(self, frame: Any) -> None:
        """Draw an opaque KMS test frame before the normal node scene."""

        stripe_width = max(1, self.width // 4)
        colors = ((196, 45, 56), (224, 151, 43), (45, 147, 92), (48, 105, 188))
        for index, color in enumerate(colors):
            left = index * stripe_width
            width = self.width - left if index == len(colors) - 1 else stripe_width
            self.pg.draw.rect(frame, color, (left, 0, width, self.height))
        if self.compact:
            panel = self.pg.Rect(24, 75, self.width - 48, 90)
            self.pg.draw.rect(frame, (8, 12, 17), panel, border_radius=12)
            self.centered_text(frame, "HEARTH", 26, TEXT, 92, True)
            self.centered_text(frame, "MINI-DISPLAY", 13, TEXT, 129)
            return
        panel = self.pg.Rect(int(self.width * .18), int(self.height * .34), int(self.width * .64), int(self.height * .32))
        self.pg.draw.rect(frame, (8, 12, 17), panel, border_radius=max(8, int(18 * self.scale)))
        self.centered_text(frame, "HEARTH", 74, TEXT, int(self.height * .40), True)
        self.centered_text(frame, "HDMI INPUT", 24, TEXT, int(self.height * .54), True)

    def draw_placeholder(self, frame: Any, rect: Any) -> None:
        self.pg.draw.rect(frame, PANEL, rect, border_radius=max(8, int(22 * self.scale)))
        for radius, alpha in ((int(rect.width * .30), 60), (int(rect.width * .18), 90), (int(rect.width * .07), 150)):
            color = tuple(int(PANEL[i] + (ACCENT[i] - PANEL[i]) * alpha / 255) for i in range(3))
            self.pg.draw.circle(frame, color, rect.center, radius, width=max(2, int(3 * self.scale)))

    def draw_playing(self, frame: Any, playback: dict[str, Any]) -> None:
        if self.compact:
            art_size = min(self.width - 88, 140)
            art_rect = self.pg.Rect((self.width - art_size) // 2, 6, art_size, art_size)
            if self.artwork_scaled:
                frame.blit(self.artwork_scaled, art_rect)
            else:
                self.draw_placeholder(frame, art_rect)

            y = art_rect.bottom + 7
            for line in self.wrapped_lines(str(playback.get("title") or "AirPlay audio"), 17, self.width - 20, 2):
                self.centered_text(frame, line, 17, TEXT, y, True)
                y += self.font(17, True).get_height()
            artist = self.wrapped_lines(
                str(playback.get("artist") or playback.get("client") or "Unknown artist"),
                12,
                self.width - 24,
                1,
            )[0]
            self.centered_text(frame, artist, 12, MUTED, min(y + 2, 202))

            duration = float(playback.get("durationSeconds") or 0)
            position = effective_position(playback)
            bar_x, bar_y, bar_width, bar_height = 12, 218, self.width - 24, 4
            self.pg.draw.rect(frame, TRACK, (bar_x, bar_y, bar_width, bar_height), border_radius=2)
            if duration > 0:
                progress = max(0.0, min(1.0, position / duration))
                self.pg.draw.rect(frame, ACCENT, (bar_x, bar_y, int(bar_width * progress), bar_height), border_radius=2)
            self.text(frame, format_duration(position), 9, MUTED, (bar_x, 226))
            right = self.font(9).render(format_duration(duration), True, MUTED)
            frame.blit(right, (self.width - bar_x - right.get_width(), 226))
            return
        margin_x = int(self.width * .08)
        art_size = min(int(self.height * .60), int(self.width * .38))
        art_rect = self.pg.Rect(margin_x, (self.height - art_size) // 2, art_size, art_size)
        if self.artwork_scaled:
            frame.blit(self.artwork_scaled, art_rect)
        else:
            self.draw_placeholder(frame, art_rect)
        x = art_rect.right + int(self.width * .05)
        content_width = self.width - x - margin_x
        y = art_rect.top + int(18 * self.scale)
        title_size = 60
        for line in self.wrapped_lines(str(playback.get("title") or "AirPlay audio"), title_size, content_width):
            self.text(frame, line, title_size, TEXT, (x, y), True)
            y += self.font(title_size, True).get_height() + int(3 * self.scale)
        y += int(15 * self.scale)
        self.text(frame, str(playback.get("artist") or playback.get("client") or "Unknown artist"), 28, TEXT, (x, y))
        y += int(46 * self.scale)
        album = str(playback.get("album") or "")
        if album:
            self.text(frame, album, 21, MUTED, (x, y))
        duration = float(playback.get("durationSeconds") or 0)
        position = effective_position(playback)
        bar_y = art_rect.bottom - int(48 * self.scale)
        bar_height = max(3, int(5 * self.scale))
        self.pg.draw.rect(frame, TRACK, (x, bar_y, content_width, bar_height), border_radius=bar_height // 2)
        if duration > 0:
            progress = max(0.0, min(1.0, position / duration))
            self.pg.draw.rect(frame, ACCENT, (x, bar_y, int(content_width * progress), bar_height), border_radius=bar_height // 2)
        label_y = bar_y + int(16 * self.scale)
        self.text(frame, format_duration(position), 13, MUTED, (x, label_y))
        right = self.font(13).render(format_duration(duration), True, MUTED)
        frame.blit(right, (x + content_width - right.get_width(), label_y))

    def render(self, state: dict[str, Any] | None, error: str = "") -> None:
        now = time.monotonic()
        if now < self.startup_until:
            self.draw_startup_test(self.screen)
            self.present()
            if not self.first_frame_logged:
                print("Hearth display startup frame drawn", flush=True)
                self.first_frame_logged = True
            return

        playback = (state or {}).get("system", {}).get("playback", {})
        scene = "playing" if playback.get("state") == "playing" else "idle"
        # Draw directly onto SDL's KMS scanout surface, as the working
        # PosterDisplay renderer does.  Avoiding an intermediate converted
        # surface also avoids an SDL2 ARGB/XRGB blit problem seen on Zero 2 W.
        frame = self.screen
        frame.blit(self.background, (0, 0))
        self.draw_playing(frame, playback) if scene == "playing" else self.draw_idle(frame)
        if error and state is None:
            message = self.font(18).render(error, True, ACCENT)
            frame.blit(message, ((self.width - message.get_width()) // 2, int(self.height * .82)))
        if scene != self.last_scene:
            self.transition_started = now
            self.last_scene = scene
        self.present()
        if not self.normal_frame_saved:
            print("Hearth display normal frame drawn", flush=True)
            self.normal_frame_saved = True


def run(config_path: Path) -> int:
    import pygame  # type: ignore

    config = load_config(config_path)
    client = HubClient(str(config.get("hubUrl", "")), str(config.get("id", "node")))
    display = NativeDisplay(pygame)
    clock = pygame.time.Clock()
    stop = threading.Event()
    workers = [
        threading.Thread(target=client.poll_forever, args=(stop,), daemon=True, name="display-state"),
        threading.Thread(target=client.heartbeat_forever, args=(stop,), daemon=True, name="display-heartbeat"),
    ]
    for worker in workers:
        worker.start()
    next_render = 0.0
    running = True
    try:
        while running:
            if display.backend != "fbdev":
                for event in pygame.event.get():
                    if event.type == pygame.QUIT:
                        running = False
            now = time.monotonic()
            if now >= next_render:
                state, error, artwork_revision, artwork_bytes = client.snapshot()
                display.update_artwork(artwork_revision, artwork_bytes)
                display.render(state, error)
                next_render = now + 0.5
            clock.tick(20)
    finally:
        stop.set()
        display.close()
    pygame.quit()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("/etc/hearth-audio/node.json"))
    args = parser.parse_args()
    os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
    os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
    return run(args.config)


if __name__ == "__main__":
    raise SystemExit(main())
