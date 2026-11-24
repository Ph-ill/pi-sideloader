from datetime import UTC, datetime

from PIL import Image

from sideload_pi.display.epd import (
    PANEL_HEIGHT,
    PANEL_WIDTH,
    RecordingIO,
    frame_buffer,
    open_panel,
    partial_refresh_enabled,
    remembered_version,
    supports_partial,
)
from sideload_pi.display.luts import V1_LUT_FULL, V1_LUT_PARTIAL, V2_LUT_FULL, V3_LUT_FULL
from sideload_pi.display.policy import RefreshBook
from sideload_pi.display.screens import render_progress


def test_lut_lengths_match_the_upstream_demos():
    assert len(V1_LUT_FULL) == 30
    assert len(V1_LUT_PARTIAL) == 30
    assert V1_LUT_FULL[0] == 0x22
    assert len(V2_LUT_FULL) == 76
    assert len(V3_LUT_FULL) == 159


def test_v1_and_v2_pack_a_black_pixel_differently():
    image = Image.new("1", (PANEL_WIDTH, PANEL_HEIGHT), 1)
    image.putpixel((0, 0), 0)
    v1 = frame_buffer(image, "V1", 0)
    v2 = frame_buffer(image, "V2", 0)
    assert len(v1) == 16 * PANEL_HEIGHT
    assert len(v2) == len(v1)
    assert v1 != v2
    assert v1[0] != 0xFF
    white = frame_buffer(Image.new("1", (250, 122), 1), "V4", 90)
    black = frame_buffer(Image.new("1", (250, 122), 0), "V4", 90)
    assert len(white) == 16 * PANEL_HEIGHT
    assert white.count(0xFF) > len(white) // 2
    assert black.count(0x00) > len(black) // 2


def test_v1_show_is_a_full_frame_even_when_partial_was_asked():
    io = RecordingIO(busy=0)
    panel = open_panel(lambda _version: io, "V1", rotate=90, busy_timeout=1)
    assert panel.version == "V1"
    assert supports_partial("V1") is False
    panel.show(render_progress("Field Notes", "ExtractingPackage", 40), False)
    commands = [event for event in io.events if isinstance(event[0], int)]
    assert commands[1] == (0x0C, bytes([0xD7, 0xD6, 0x9D]))
    assert (0x32, bytes(V1_LUT_FULL)) in commands
    assert sum(1 for command, _data in commands if command == 0x24) == PANEL_HEIGHT
    assert (0x22, bytes([0xC4])) in commands
    assert (0xFF, b"") in commands
    assert (0x22, bytes([0xFF])) not in commands


def test_auto_falls_through_to_v1_when_newer_inits_stay_busy():
    seen = []

    def factory(version):
        seen.append(version)
        return RecordingIO(busy=0 if version == "V1" else 1)

    panel = open_panel(factory, "auto", busy_timeout=0.05)
    assert seen == ["V4", "V3", "V2", "V1"]
    assert panel.version == "V1"


def test_display_test_preview_does_not_need_the_panel(tmp_path, monkeypatch):
    from sideload_pi.cli import main

    monkeypatch.delenv("SIDELOAD_PI_CONFIG", raising=False)
    dest = tmp_path / "card.png"
    assert main(["display-test", "V1", "--preview", str(dest)]) == 0
    assert dest.stat().st_size > 100


def test_partial_policy_and_saved_version(tmp_path):
    path = tmp_path / "display-version"
    path.write_text("V1\n")
    assert remembered_version(path) == "V1"
    assert partial_refresh_enabled("V1", True) is False
    assert partial_refresh_enabled("V3", True) is True
    assert partial_refresh_enabled("V3", False) is False
    assert partial_refresh_enabled("auto", True) is True
    now = datetime(2026, 1, 1, tzinfo=UTC)
    book = RefreshBook()
    assert book.choose("installing", now, every=8, minutes=30, partial_ok=True) is True
    assert book.choose("installing", now, every=8, minutes=30, partial_ok=True) is False
    assert book.choose("installing", now, every=8, minutes=30, partial_ok=False) is True
