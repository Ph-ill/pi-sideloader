from datetime import UTC, datetime, timedelta
from pathlib import Path

from PIL import Image

from sideload_pi.display.screens import (
    BAR,
    HEIGHT,
    WIDTH,
    gallery,
    render_error,
    render_progress,
    render_test_pattern,
    rows_from_apps,
)
from sideload_pi.library import AppRecord
from sideload_pi.system import Hud

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
PREVIEWS = Path(__file__).resolve().parents[1] / "docs" / "previews"


def _black(image: Image.Image) -> int:
    x, y, width, height = BAR
    count = 0
    for py in range(y, y + height):
        for px in range(x, x + width):
            if image.getpixel((px, py)) == 0:
                count += 1
    return count


def test_progress_bar_grows_and_the_canvas_is_the_panel():
    empty = render_progress("Field Notes", "ExtractingPackage", 0)
    half = render_progress("Field Notes", "ExtractingPackage", 50)
    full = render_progress("Field Notes", "ExtractingPackage", 100)
    assert empty.size == (WIDTH, HEIGHT)
    assert _black(empty) < _black(half) < _black(full)


def test_error_screen_redacts_a_password():
    shown = render_error("password=hunter2")
    expected = render_error("password=[redacted]")
    assert shown.tobytes() == expected.tobytes()


def test_rows_mark_retry_due_and_ok():
    apps = [
        AppRecord("a.b.c", "A", "1", "a.ipa", expiry=NOW + timedelta(days=6)),
        AppRecord("d.e.f", "D", "1", "d.ipa", expiry=NOW + timedelta(hours=3)),
        AppRecord(
            "g.h.i",
            "G",
            "1",
            "g.ipa",
            expiry=NOW + timedelta(days=6),
            last_error="nope",
            next_retry_at=NOW + timedelta(hours=1),
        ),
    ]
    rows = rows_from_apps(apps, NOW, 1)
    assert [row["status"] for row in rows] == ["ok", "due", "err"]


def test_committed_previews_match_this_pillow():
    images = gallery(NOW)
    assert set(images) >= {"idle", "progress", "needs-2fa", "waiting", "low-battery"}
    for name, image in images.items():
        saved = Image.open(PREVIEWS / f"{name}.png").convert("1")
        assert image.tobytes() == saved.tobytes(), name
    for version in ("V1", "V2", "V3", "V4"):
        pattern = render_test_pattern(version)
        saved = Image.open(PREVIEWS / f"test-{version.lower()}.png").convert("1")
        assert pattern.tobytes() == saved.tobytes()
        assert pattern.size == (WIDTH, HEIGHT)


def test_hud_accepts_the_idle_fields():
    hud = Hud(battery_percent=86, charging=True, wifi_up=True, ip="100.64.1.8", last_refresh=NOW)
    assert hud.ip.startswith("100.")
