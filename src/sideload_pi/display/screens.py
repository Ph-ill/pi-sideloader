"""1-bit screens for a 250x122 panel, also saved as PNG without hardware.

The landscape image is what a person sees with the HAT on a Pi Zero. The
driver rotates it into the panel's 122x250 buffer.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from sideload_pi.redact import redact
from sideload_pi.scheduling import whole_days_remaining

WIDTH = 250
HEIGHT = 122
# Inner progress bar. Tests measure black pixels in this rectangle.
BAR = (16, 74, 218, 14)


def _font():
    return ImageFont.load_default()


def _canvas() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("1", (WIDTH, HEIGHT), 1)
    return image, ImageDraw.Draw(image)


def _text_width(draw, text: str, font) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def _fit(draw, text: str, font, width: int) -> str:
    if _text_width(draw, text, font) <= width:
        return text
    trimmed = text
    while trimmed and _text_width(draw, trimmed + "...", font) > width:
        trimmed = trimmed[:-1]
    return (trimmed + "...") if trimmed else ""


def _wrap(draw, text: str, font, width: int, limit: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = word if not current else f"{current} {word}"
        if _text_width(draw, trial, font) <= width:
            current = trial
            continue
        if current:
            lines.append(current)
        current = _fit(draw, word, font, width)
        if len(lines) >= limit:
            break
    if current and len(lines) < limit:
        lines.append(current)
    return lines[:limit]


def _header(draw, font, title: str, right: str) -> int:
    draw.rectangle((0, 0, WIDTH - 1, 16), fill=0)
    draw.text((4, 3), _fit(draw, title, font, 150), font=font, fill=1)
    right_w = _text_width(draw, right, font)
    draw.text((WIDTH - 4 - right_w, 3), right, font=font, fill=1)
    return 20


def _battery_label(percent: float | None, charging: bool | None) -> str:
    if percent is None:
        label = "--"
    else:
        label = f"{int(round(percent))}%"
    if charging is True:
        return f"{label} CHG"
    if charging is False:
        return f"{label} BAT"
    return label


def relative_time(moment: datetime | None, now: datetime) -> str:
    if moment is None:
        return "never"
    seconds = int((now - moment).total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 45:
        return "just now"
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def render_idle(rows: list[dict], hud, now: datetime, notice: str = "") -> Image.Image:
    image, draw = _canvas()
    font = _font()
    _header(draw, font, "sideload-pi", _battery_label(hud.battery_percent, hud.charging))
    wifi = "wifi" if hud.wifi_up else ("no wifi" if hud.wifi_up is False else "wifi ?")
    ip = hud.ip or "no ip"
    draw.text((4, 20), _fit(draw, f"{wifi}  {ip}", font, WIDTH - 8), font=font, fill=0)
    y = 36
    if not rows:
        draw.text((4, y), "No IPAs yet", font=font, fill=0)
        draw.text((4, y + 14), "Add one over SSH or the web UI", font=font, fill=0)
    else:
        draw.text((4, y), "app", font=font, fill=0)
        draw.text((150, y), "left", font=font, fill=0)
        draw.text((190, y), "state", font=font, fill=0)
        y += 12
        for row in rows[:4]:
            days = row.get("days")
            day_text = "new" if days is None else f"{days}d"
            draw.text((4, y), _fit(draw, str(row["name"]), font, 140), font=font, fill=0)
            draw.text((150, y), day_text, font=font, fill=0)
            draw.text((190, y), str(row.get("status") or ""), font=font, fill=0)
            y += 12
        extra = len(rows) - 4
        if extra > 0:
            draw.text((4, y), f"+{extra} more", font=font, fill=0)
    footer = notice or f"refreshed {relative_time(hud.last_refresh, now)}"
    draw.line((0, HEIGHT - 16, WIDTH, HEIGHT - 16), fill=0)
    draw.text((4, HEIGHT - 14), _fit(draw, footer, font, WIDTH - 8), font=font, fill=0)
    return image


def render_progress(name: str, stage: str, percent: int | None) -> Image.Image:
    image, draw = _canvas()
    font = _font()
    shown = 0 if percent is None else max(0, min(100, int(percent)))
    _header(draw, font, "Installing", f"{shown}%")
    draw.text((4, 24), _fit(draw, name, font, WIDTH - 8), font=font, fill=0)
    draw.text((4, 42), _fit(draw, stage or "Transferring", font, WIDTH - 8), font=font, fill=0)
    x, y, width, height = BAR
    draw.rectangle((x, y, x + width - 1, y + height - 1), outline=0, fill=1)
    inner = width - 4
    filled = int(inner * shown / 100)
    if filled > 0:
        draw.rectangle((x + 2, y + 2, x + 2 + filled - 1, y + height - 3), fill=0)
    draw.text((4, 96), "USB or Wi-Fi install", font=font, fill=0)
    return image


def render_stage(title: str, name: str, detail: str) -> Image.Image:
    image, draw = _canvas()
    font = _font()
    _header(draw, font, title, "")
    draw.text((4, 28), _fit(draw, name, font, WIDTH - 8), font=font, fill=0)
    for index, line in enumerate(_wrap(draw, detail, font, WIDTH - 8, 4)):
        draw.text((4, 48 + index * 12), line, font=font, fill=0)
    return image


def render_success(name: str, days: int | None) -> Image.Image:
    image, draw = _canvas()
    font = _font()
    _header(draw, font, "Installed", "")
    draw.text((4, 32), _fit(draw, name, font, WIDTH - 8), font=font, fill=0)
    if days is None:
        line = "Profile expiry was not reported"
    else:
        line = f"Opens for {days} day{'s' if days != 1 else ''}"
    draw.text((4, 56), line, font=font, fill=0)
    draw.text((4, 80), "Trust the developer if iOS asks", font=font, fill=0)
    return image


def render_error(message: str) -> Image.Image:
    image, draw = _canvas()
    font = _font()
    _header(draw, font, "Needs attention", "")
    safe = redact(message)
    for index, line in enumerate(_wrap(draw, safe, font, WIDTH - 8, 6)):
        draw.text((4, 24 + index * 13), line, font=font, fill=0)
    return image


def render_2fa() -> Image.Image:
    return render_stage(
        "Apple ID code",
        "Six digits",
        "Enter it with sudo sideload-pi 2fa 123456 or the web page. It is not stored.",
    )


def render_waiting(due: int) -> Image.Image:
    noun = "app" if due == 1 else "apps"
    return render_stage(
        "Waiting for iPhone",
        f"{due} {noun} due",
        "USB port (not PWR): OTG hub, then USB-A to USB-C. Tap Trust. Keep the UPS charger connected so the phone does not brown out the Pi.",
    )


def render_low_battery(percent: float | None, shutting_down: bool) -> Image.Image:
    label = "--" if percent is None else f"{int(round(percent))}%"
    detail = "Shutting down so the SD card is not cut off." if shutting_down else "Plug in the UPS."
    return render_stage("Battery low", label, detail)


def rows_from_apps(apps, now: datetime, margin_days: float) -> list[dict]:
    from datetime import timedelta

    margin = timedelta(days=margin_days)
    rows = []
    for app in apps:
        days = whole_days_remaining(app.expiry, now)
        waiting_retry = bool(app.last_error) and (
            app.next_retry_at is None or app.next_retry_at > now
        )
        if waiting_retry:
            status = "err"
        elif app.expiry is None:
            status = "new"
        elif days == 0 or (app.expiry - now) <= margin:
            status = "due"
        else:
            status = "ok"
        rows.append({"name": app.name, "days": days, "status": status})
    return rows


def render_test_pattern(version: str) -> Image.Image:
    """One full-frame card per driver. A matching panel shows this text crisply.

    V1's published driver has no partial update, so the card says so. The
    bars and the double border make a wrong rotation or a wrong LUT obvious:
    the picture turns into snow, or the label is not the version you asked for.
    """
    image, draw = _canvas()
    font = _font()
    draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), outline=0)
    draw.rectangle((2, 2, WIDTH - 3, HEIGHT - 3), outline=0)
    _header(draw, font, f"TEST {version}", "250x122")
    partial = "partial refresh" if version in {"V2", "V3", "V4"} else "full refresh only"
    draw.text((6, 22), _fit(draw, partial, font, WIDTH - 12), font=font, fill=0)
    for index in range(4):
        left = 8 + index * 24
        bar_h = 8 + index * 6
        draw.rectangle((left, 68 - bar_h, left + 14, 68), fill=0)
    draw.text((120, 36), version, font=font, fill=0)
    draw.text((120, 52), "bars solid?", font=font, fill=0)
    draw.text((6, 80), "Crisp label: set display.version to this.", font=font, fill=0)
    draw.text((6, 96), "Noise or blank: power-cycle, try next.", font=font, fill=0)
    return image


def write_test_patterns(directory: Path) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for version in ("V1", "V2", "V3", "V4"):
        path = directory / f"test-{version.lower()}.png"
        render_test_pattern(version).save(path)
        written.append(path)
    return written


def gallery(now: datetime) -> dict[str, Image.Image]:
    """Every screen the panel can show, for docs and tests."""
    from sideload_pi.system import Hud

    hud = Hud(
        battery_percent=86,
        charging=True,
        wifi_up=True,
        ip="100.64.1.8",
        last_refresh=now,
    )
    rows = [
        {"name": "Field Notes", "days": 6, "status": "ok"},
        {"name": "Meter", "days": 1, "status": "due"},
        {"name": "Sketch", "days": None, "status": "new"},
    ]
    return {
        "idle": render_idle(rows, hud, now),
        "idle-empty": render_idle([], Hud(battery_percent=40, charging=False, wifi_up=False), now),
        "provisioning": render_stage("Signing in to Apple", "Field Notes", "Registering this iPhone and refreshing the 7-day profile."),
        "signing": render_stage("Signing", "Field Notes", "zsign is re-signing the IPA with your development certificate."),
        "progress": render_progress("Field Notes", "ExtractingPackage", 42),
        "success": render_success("Field Notes", 7),
        "error": render_error("Install failed: a valid provisioning profile for this executable was not found."),
        "needs-2fa": render_2fa(),
        "waiting": render_waiting(2),
        "low-battery": render_low_battery(6, True),
    }


def write_gallery(directory: Path, now: datetime) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, image in gallery(now).items():
        path = directory / f"{name}.png"
        image.save(path)
        written.append(path)
    return written
