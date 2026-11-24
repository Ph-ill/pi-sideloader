"""Full versus partial refresh, and how often the progress bar is repainted."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class RefreshBook:
    partials_since_full: int = 0
    last_full: datetime | None = None
    last_kind: str = ""

    def choose(
        self,
        kind: str,
        now: datetime,
        *,
        every: int,
        minutes: int,
        partial_ok: bool = True,
    ) -> bool:
        """Return True when this frame must be a full refresh.

        A full refresh flashes the panel and clears ghosting. Waveshare's
        guidance for the V2/V3/V4 panels is to full-refresh after several
        partials (their note says about five). Kind changes (idle, signing,
        progress, error) also full-refresh because the whole layout changed.
        V1 has no partial-refresh display path, so every frame is full.
        """
        if not partial_ok:
            self.partials_since_full = 0
            self.last_full = now
            self.last_kind = kind
            return True
        changed = kind != self.last_kind
        stale = self.last_full is None or (now - self.last_full) >= timedelta(minutes=minutes)
        count = self.partials_since_full >= every
        full = self.last_kind == "" or changed or stale or count
        if full:
            self.partials_since_full = 0
            self.last_full = now
        else:
            self.partials_since_full += 1
        self.last_kind = kind
        return full


def should_repaint(
    previous: int | None,
    percent: int | None,
    last_paint: datetime | None,
    now: datetime,
    *,
    min_delta: int = 5,
    min_seconds: float = 2,
) -> bool:
    """Skip e-ink updates that would only move the bar by a pixel or two."""
    if percent is None or previous is None or last_paint is None:
        return True
    if percent >= 100 or percent < previous:
        return True
    if percent - previous >= min_delta:
        return True
    elapsed = (now - last_paint).total_seconds()
    return elapsed >= min_seconds and percent != previous
