"""YouTube: there is no clips API, so we look at a creator's recent uploads/VODs and cut out
the "most replayed" section of each (the heatmap shown on the player's seek bar)."""
from __future__ import annotations

from datetime import datetime, timezone

import yt_dlp

from ..config import Config, Creator
from ..db import Clip
from ..rank import age_hours, best_window, velocity

RECENT_VIDEOS = 5
WINDOW = 45.0  # seconds


def _channel_url(channel: str) -> str:
    if channel.startswith("http"):
        return channel.rstrip("/")
    return f"https://www.youtube.com/{channel if channel.startswith('@') else '@' + channel}"


def discover(cfg: Config, creators: list[Creator]) -> list[Clip]:
    now = datetime.now(timezone.utc)
    out = []
    quiet = {"quiet": True, "no_warnings": True, "skip_download": True}
    for c in creators:
        with yt_dlp.YoutubeDL({**quiet, "extract_flat": True, "playlistend": RECENT_VIDEOS}) as ydl:
            listing = ydl.extract_info(_channel_url(c.channel) + "/videos", download=False)
        for entry in listing.get("entries") or []:
            title = entry.get("title") or ""
            if cfg.keywords and not any(k.lower() in title.lower() for k in cfg.keywords):
                continue
            with yt_dlp.YoutubeDL(quiet) as ydl:
                info = ydl.extract_info(entry["url"], download=False)
            ts = info.get("timestamp")
            if not ts or not info.get("heatmap"):
                continue  # heatmaps only exist once a video has enough views
            created = datetime.fromtimestamp(ts, timezone.utc).isoformat()
            views = info.get("view_count") or 0
            if views < cfg.min_views or age_hours(created, now) > cfg.lookback_hours:
                continue
            try:
                start, end, peak = best_window(info["heatmap"], min(WINDOW, cfg.max_duration))
            except ValueError:
                continue
            out.append(Clip(
                id=f"youtube:{info['id']}", platform="youtube", creator=c.channel, credit=c.credit,
                title=title, url=info["webpage_url"], views=views, created_at=created,
                duration=end - start, start=start, end=end,
                score=velocity(views, created, now) * peak,
            ))
    return out
