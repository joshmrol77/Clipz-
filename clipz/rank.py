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
