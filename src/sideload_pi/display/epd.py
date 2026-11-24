"""Waveshare 2.13inch e-Paper HAT, revisions V1, V2, V3, and V4.

Init sequences and LUT bytes are adapted from Waveshare's MIT-licensed demos
(epd2in13.py, epd2in13_V2.py, epd2in13_V3.py, epd2in13_V4.py). See
third_party/WAVESHARE.txt. All four black/white panels are 250x122 landscape
(controller buffer 122x250). The panel has no version register. The sticker
on the back of the screen, not the HAT's Rev2.x silkscreen, says which driver
to use. Confirm with `sideload-pi display-test V1` (and V2, V3, V4).

"auto" tries V4, then V3, then V2, and only then V1. V4/V3/V2 init waits on
BUSY, so a stuck line fails that candidate. V1's init (epd2in13.py) never
reads BUSY, so a V1 probe cannot fail closed. Do not trust auto on a panel
that might be V1.

Pins, BCM, matching the HAT: DC 25, RST 17, BUSY 24, CS 8, SPI0.
Busy is high while the controller is refreshing and low when it is idle.

V2, V3, and V4 have a partial refresh. V1's demo ships a partial LUT but
display() always writes the whole frame and activates it with 0xC4. This
driver does not use that LUT. Progress on V1 is a throttled full refresh.
"""

from __future__ import annotations

import time
from pathlib import Path

from PIL import Image

from sideload_pi.display.luts import (
    V1_LUT_FULL,
    V2_LUT_FULL,
    V2_LUT_PARTIAL,
    V3_LUT_FULL,
    V3_LUT_PARTIAL,
)
from sideload_pi.errors import SideloadError

PANEL_WIDTH = 122
PANEL_HEIGHT = 250
UI_SIZE = (250, 122)
DRIVERS = ("V1", "V2", "V3", "V4")
# V2/V3/V4 demos have display_Partial / a fast-refresh waveform. V1 does not.
PARTIAL_DRIVERS = frozenset({"V2", "V3", "V4"})


def supports_partial(version: str) -> bool:
    """True when this driver's published update path can refresh without a full flash."""
    return version in PARTIAL_DRIVERS


def partial_refresh_enabled(version: str, allow_partial: bool) -> bool:
    """Config can force full refreshes. Unknown auto (no panel yet) keeps the partial policy."""
    if not allow_partial:
        return False
    if version == "auto":
        return True
    return supports_partial(version)


class DisplayTimeout(SideloadError):
    def __init__(self, version: str):
        self.version = version
        super().__init__(f"e-paper {version} stayed busy. Check display.version against the sticker.")


class RecordingIO:
    """In-memory panel used by tests. `busy` can stay high to simulate a wrong revision."""

    def __init__(self, busy: int = 0):
        self.busy = busy
        self.events: list[tuple] = []

    def reset(self, slow: bool = False) -> None:
        self.events.append(("reset", slow))

    def command(self, command: int, data: bytes = b"") -> None:
        self.events.append((command, bytes(data)))

    def read_busy(self) -> int:
        return self.busy

    def sleep(self, seconds: float) -> None:
        return None

    def close(self) -> None:
        return None


class LgpioIO:
    def __init__(self, config):
        import lgpio
        import spidev

        self.lgpio = lgpio
        self.dc = config.display.dc_pin
        self.rst = config.display.rst_pin
        self.busy_pin = config.display.busy_pin
        self.cs = config.display.cs_pin
        self.handle = lgpio.gpiochip_open(0)
        for pin in (self.dc, self.rst, self.cs):
            lgpio.gpio_claim_output(self.handle, pin, 1)
        lgpio.gpio_claim_input(self.handle, self.busy_pin)
        self.spi = spidev.SpiDev()
        self.spi.open(config.display.spi_bus, config.display.spi_device)
        self.spi.max_speed_hz = 4_000_000
        self.spi.mode = 0

    def reset(self, slow: bool = False) -> None:
        high, low = (0.2, 0.005) if slow else (0.02, 0.002)
        self.lgpio.gpio_write(self.handle, self.rst, 1)
        time.sleep(high)
        self.lgpio.gpio_write(self.handle, self.rst, 0)
        time.sleep(low)
        self.lgpio.gpio_write(self.handle, self.rst, 1)
        time.sleep(high)

    def command(self, command: int, data: bytes = b"") -> None:
        self.lgpio.gpio_write(self.handle, self.dc, 0)
        self.lgpio.gpio_write(self.handle, self.cs, 0)
        self.spi.writebytes([command & 0xFF])
        self.lgpio.gpio_write(self.handle, self.cs, 1)
        if not data:
            return
        self.lgpio.gpio_write(self.handle, self.dc, 1)
        self.lgpio.gpio_write(self.handle, self.cs, 0)
        view = list(data)
        for start in range(0, len(view), 4096):
            self.spi.writebytes(view[start : start + 4096])
        self.lgpio.gpio_write(self.handle, self.cs, 1)

    def read_busy(self) -> int:
        return int(self.lgpio.gpio_read(self.handle, self.busy_pin))

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def close(self) -> None:
        try:
            self.spi.close()
        finally:
            self.lgpio.gpiochip_close(self.handle)


def frame_buffer(image: Image.Image, version: str, rotate: int) -> bytes:
    if image.size == UI_SIZE and rotate:
        image = image.rotate(rotate, expand=True)
    image = image.convert("1")
    if version == "V1":
        return bytes(_pack_v1(image))
    if version == "V2":
        return bytes(_pack_v2(image))
    return image.tobytes()


def _pack_v1(image: Image.Image) -> bytearray:
    """Port of Waveshare epd2in13.getbuffer. Unlike V2, black pixels are not mirrored."""
    width, height = image.size
    linewidth = PANEL_WIDTH // 8 if PANEL_WIDTH % 8 == 0 else PANEL_WIDTH // 8 + 1
    buf = bytearray([0xFF] * (linewidth * PANEL_HEIGHT))
    pixels = image.load()
    if width == PANEL_WIDTH and height == PANEL_HEIGHT:
        for y in range(height):
            for x in range(width):
                if pixels[x, y] == 0:
                    buf[int(x / 8) + y * linewidth] &= ~(0x80 >> (x % 8))
    elif width == PANEL_HEIGHT and height == PANEL_WIDTH:
        for y in range(height):
            for x in range(width):
                if pixels[x, y] != 0:
                    continue
                newx = y
                newy = PANEL_HEIGHT - x - 1
                index = int(newx / 8) + newy * linewidth
                if 0 <= index < len(buf):
                    buf[index] &= ~(0x80 >> (y % 8))
    return buf


def _pack_v2(image: Image.Image) -> bytearray:
    """Port of Waveshare epd2in13_V2.getbuffer for a 122x250 or 250x122 image."""
    width, height = image.size
    stride = PANEL_WIDTH // 8 + 1
    buf = bytearray([0xFF] * (stride * PANEL_HEIGHT))
    pixels = image.load()
    if width == PANEL_WIDTH and height == PANEL_HEIGHT:
        for y in range(height):
            for x in range(width):
                if pixels[x, y] == 0:
                    mirrored = width - x
                    index = int(mirrored / 8) + y * stride
                    if 0 <= index < len(buf):
                        buf[index] &= ~(0x80 >> (mirrored % 8))
    elif width == PANEL_HEIGHT and height == PANEL_WIDTH:
        for y in range(height):
            for x in range(width):
                if pixels[x, y] != 0:
                    continue
                newx = y
                newy = PANEL_HEIGHT - x - 1
                newy = width - newy - 1
                index = int(newx / 8) + newy * stride
                if 0 <= index < len(buf):
                    buf[index] &= ~(0x80 >> (y % 8))
    return buf


class Panel:
    def __init__(self, io, version: str, rotate: int = 90, busy_timeout: float = 20):
        if version not in DRIVERS:
            raise SideloadError(f"unknown e-paper version {version}")
        self.io = io
        self.version = version
        self.rotate = rotate
        self.busy_timeout = busy_timeout
        self._ready = False

    def init(self) -> None:
        if self.version == "V1":
            self._init_v1()
        elif self.version == "V2":
            self._init_v2()
        elif self.version == "V3":
            self._init_v3()
        else:
            self._init_v4()
        self._ready = True

    def show(self, image: Image.Image, full: bool) -> None:
        if not self._ready:
            self.init()
        # V1's display() has no partial path. A requested partial is a full frame.
        if not supports_partial(self.version):
            full = True
        buf = frame_buffer(image, self.version, self.rotate)
        if self.version == "V1":
            self._show_v1(buf)
        elif self.version == "V2":
            self._show_v2(buf, full)
        elif self.version == "V3":
            self._show_v3(buf, full)
        else:
            self._show_v4(buf, full)

    def sleep(self) -> None:
        self.io.command(0x10, bytes([0x01]))
        self.io.sleep(0.2)

    def close(self) -> None:
        self.io.close()

    def _wait(self) -> None:
        deadline = time.monotonic() + self.busy_timeout
        while self.io.read_busy() == 1:
            if time.monotonic() > deadline:
                raise DisplayTimeout(self.version)
            self.io.sleep(0.01)

    def _init_v1(self) -> None:
        # epd2in13.init(lut_full_update). This sequence does not read BUSY.
        self.io.reset(True)
        self.io.command(0x01, bytes([(PANEL_HEIGHT - 1) & 0xFF, ((PANEL_HEIGHT - 1) >> 8) & 0xFF, 0x00]))
        self.io.command(0x0C, bytes([0xD7, 0xD6, 0x9D]))
        self.io.command(0x2C, bytes([0xA8]))
        self.io.command(0x3A, bytes([0x1A]))
        self.io.command(0x3B, bytes([0x08]))
        self.io.command(0x3C, bytes([0x03]))
        self.io.command(0x11, bytes([0x03]))
        self.io.command(0x32, bytes(V1_LUT_FULL))

    def _show_v1(self, buf: bytes) -> None:
        # epd2in13.display + TurnOnDisplay. SetWindows(0, 0, width, height)
        # passes the dimensions, not dimension-1, and each row is its own 0x24.
        linewidth = PANEL_WIDTH // 8 if PANEL_WIDTH % 8 == 0 else PANEL_WIDTH // 8 + 1
        self.io.command(0x44, bytes([0x00, (PANEL_WIDTH >> 3) & 0xFF]))
        self.io.command(
            0x45,
            bytes([0x00, 0x00, PANEL_HEIGHT & 0xFF, (PANEL_HEIGHT >> 8) & 0xFF]),
        )
        for row in range(PANEL_HEIGHT):
            self.io.command(0x4E, bytes([0x00]))
            self.io.command(0x4F, bytes([row & 0xFF, (row >> 8) & 0xFF]))
            self._wait()
            start = row * linewidth
            self.io.command(0x24, buf[start : start + linewidth])
        self.io.command(0x22, bytes([0xC4]))
        self.io.command(0x20)
        self.io.command(0xFF)
        self._wait()

    def _init_v4(self) -> None:
        self.io.reset(False)
        self._wait()
        self.io.command(0x12)
        self._wait()
        self.io.command(0x01, bytes([0xF9, 0x00, 0x00]))
        self.io.command(0x11, bytes([0x03]))
        self._window_cursor()
        self.io.command(0x3C, bytes([0x05]))
        self.io.command(0x21, bytes([0x00, 0x80]))
        self.io.command(0x18, bytes([0x80]))
        self._wait()

    def _init_v3(self) -> None:
        self.io.reset(False)
        self._wait()
        self.io.command(0x12)
        self._wait()
        self.io.command(0x01, bytes([0xF9, 0x00, 0x00]))
        self.io.command(0x11, bytes([0x03]))
        self._window_cursor()
        self.io.command(0x3C, bytes([0x05]))
        self.io.command(0x21, bytes([0x00, 0x80]))
        self.io.command(0x18, bytes([0x80]))
        self._wait()
        self._lut_v3(V3_LUT_FULL)

    def _init_v2(self) -> None:
        self.io.reset(True)
        self._wait()
        self.io.command(0x12)
        self._wait()
        self.io.command(0x74, bytes([0x54]))
        self.io.command(0x7E, bytes([0x3B]))
        self.io.command(0x01, bytes([0xF9, 0x00, 0x00]))
        self.io.command(0x11, bytes([0x01]))
        self.io.command(0x44, bytes([0x00, 0x0F]))
        self.io.command(0x45, bytes([0xF9, 0x00, 0x00, 0x00]))
        self.io.command(0x3C, bytes([0x03]))
        self.io.command(0x2C, bytes([0x55]))
        lut = V2_LUT_FULL
        self.io.command(0x03, bytes([lut[70]]))
        self.io.command(0x04, bytes(lut[71:74]))
        self.io.command(0x3A, bytes([lut[74]]))
        self.io.command(0x3B, bytes([lut[75]]))
        self.io.command(0x32, bytes(lut[:70]))
        self.io.command(0x4E, bytes([0x00]))
        self.io.command(0x4F, bytes([0xF9, 0x00]))
        self._wait()

    def _show_v4(self, buf: bytes, full: bool) -> None:
        if not full:
            self.io.reset(False)
            self.io.command(0x3C, bytes([0x80]))
            self.io.command(0x01, bytes([0xF9, 0x00, 0x00]))
            self.io.command(0x11, bytes([0x03]))
            self._window_cursor()
        self.io.command(0x24, buf)
        if full:
            self.io.command(0x22, bytes([0xF7]))
        else:
            self.io.command(0x22, bytes([0xFF]))
        self.io.command(0x20)
        self._wait()

    def _show_v3(self, buf: bytes, full: bool) -> None:
        if not full:
            self.io.reset(False)
            self._lut_v3(V3_LUT_PARTIAL)
            self.io.command(0x37, bytes([0x00, 0x00, 0x00, 0x00, 0x00, 0x40, 0x00, 0x00, 0x00, 0x00]))
            self.io.command(0x3C, bytes([0x80]))
            self.io.command(0x22, bytes([0xC0]))
            self.io.command(0x20)
            self._wait()
            self._window_cursor()
        self.io.command(0x24, buf)
        self.io.command(0x22, bytes([0xC7 if full else 0x0F]))
        self.io.command(0x20)
        self._wait()

    def _show_v2(self, buf: bytes, full: bool) -> None:
        if not full:
            self.io.command(0x2C, bytes([0x26]))
            self._wait()
            self.io.command(0x32, bytes(V2_LUT_PARTIAL[:70]))
            self.io.command(0x37, bytes([0x00, 0x00, 0x00, 0x00, 0x40, 0x00, 0x00]))
            self.io.command(0x22, bytes([0xC0]))
            self.io.command(0x20)
            self._wait()
            self.io.command(0x3C, bytes([0x01]))
            inverted = bytes(byte ^ 0xFF for byte in buf)
            self.io.command(0x24, buf)
            self.io.command(0x26, inverted)
            self.io.command(0x22, bytes([0x0C]))
        else:
            self.io.command(0x24, buf)
            self.io.command(0x22, bytes([0xC7]))
        self.io.command(0x20)
        self._wait()

    def _window_cursor(self) -> None:
        self.io.command(0x44, bytes([0x00, (PANEL_WIDTH - 1) >> 3]))
        self.io.command(0x45, bytes([0x00, 0x00, (PANEL_HEIGHT - 1) & 0xFF, (PANEL_HEIGHT - 1) >> 8]))
        self.io.command(0x4E, bytes([0x00]))
        self.io.command(0x4F, bytes([0x00, 0x00]))

    def _lut_v3(self, lut: list[int]) -> None:
        self.io.command(0x32, bytes(lut[:153]))
        self._wait()
        self.io.command(0x3F, bytes([lut[153]]))
        self.io.command(0x03, bytes([lut[154]]))
        self.io.command(0x04, bytes([lut[155], lut[156], lut[157]]))
        self.io.command(0x2C, bytes([lut[158]]))


def open_panel(io_factory, version: str, rotate: int = 90, saved: str | None = None, busy_timeout: float = 20) -> Panel:
    if version == "auto":
        order = [item for item in (saved, "V4", "V3", "V2", "V1") if item]
        # de-duplicate while keeping order
        seen = []
        for item in order:
            if item not in seen:
                seen.append(item)
        order = seen
    else:
        order = [version]
    last: Exception | None = None
    for candidate in order:
        panel = Panel(io_factory(candidate), candidate, rotate=rotate, busy_timeout=busy_timeout)
        try:
            panel.init()
        except DisplayTimeout as exc:
            last = exc
            panel.close()
            continue
        return panel
    if last:
        raise last
    raise SideloadError("no e-paper version to try")


def remembered_version(path: Path) -> str | None:
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text if text in DRIVERS else None
