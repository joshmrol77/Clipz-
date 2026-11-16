from __future__ import annotations

from datetime import datetime, timezone


def age_hours(created_at: str, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return max((now - created).total_seconds() / 3600, 0.0)


def velocity(views: int, created_at: str, now: datetime | None = None) -> float:
    """Views per hour, damped so a 5-minute-old clip with 20 views doesn't beat everything."""
    return views / (age_hours(created_at, now) + 2.0)


VIRAL_HEAT = 3.0          # at least this many times faster than the typical clip in its group...
VIRAL_MIN_SPEED = 300     # ...and gaining at least this many views an hour
VIRAL_ANY_SPEED = 5000    # or exploding regardless of the group


def views_per_hour(views: int, created_at: str, now: datetime | None = None) -> float:
    return views / max(age_hours(created_at, now), 0.5)


def add_heat(clips: list, group_of, now: datetime | None = None) -> None:
    """Set clip.heat = its views-per-hour divided by the median views-per-hour of its group
    (same streamer, or same followed game). A clip doing 10x its streamer's usual is a breakout."""
    from statistics import median
    groups: dict = {}
    for c in clips:
        groups.setdefault(group_of(c), []).append(c)
    for members in groups.values():
        speeds = [views_per_hour(c.views, c.created_at, now) for c in members if c.created_at]
        typical = max(median(speeds), 1.0) if speeds else 1.0
        for c in members:
            c.heat = round(views_per_hour(c.views, c.created_at, now) / typical, 2) if c.created_at else 0.0


def is_viral(clip, now: datetime | None = None) -> bool:
    speed = views_per_hour(clip.views, clip.created_at, now) if clip.created_at else 0
    return speed >= VIRAL_ANY_SPEED or (clip.heat >= VIRAL_HEAT and speed >= VIRAL_MIN_SPEED)


def best_window(heatmap: list[dict], length: float, skip_before: float = 30.0) -> tuple[float, float, float]:
    """Pick the most-replayed `length`-second window from a YouTube heatmap.

    heatmap: [{"start_time", "end_time", "value"}] in order. The first seconds of every video
    are always "most replayed", so windows starting before `skip_before` are ignored.
    Returns (start, end, mean_value).
    """
    pts = [p for p in heatmap if p["start_time"] >= skip_before]
    if not pts:
        raise ValueError("heatmap has no usable points")
    best = (-1.0, 0.0, 0.0)
    for i, p in enumerate(pts):
        start = p["start_time"]
        vals = [q["value"] for q in pts[i:] if q["start_time"] < start + length]
        mean = sum(vals) / len(vals)
        if mean > best[0]:
            best = (mean, start, min(start + length, pts[-1]["end_time"]))
    mean, start, end = best
    return start, end, mean
