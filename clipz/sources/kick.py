"""Kick clips. EXPERIMENTAL: Kick has no documented public clips API, so this uses the same
JSON endpoint the website does. It can be blocked by Cloudflare or change without notice;
when it fails, add Kick clips by hand with `clipz add <url>`."""
from __future__ import annotations

from datetime import datetime, timezone

import requests

from ..config import Config, Creator
from ..db import Clip
from ..rank import age_hours, velocity


def discover(cfg: Config, creators: list[Creator]) -> list[Clip]:
    now = datetime.now(timezone.utc)
    out, errors = [], []
    for c in creators:
        try:
            r = requests.get(
                f"https://kick.com/api/v2/channels/{c.channel}/clips",
                params={"sort": "view", "time": "day"},
                headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
                timeout=30,
            )
            r.raise_for_status()
            clips = r.json().get("clips", [])
        except Exception as e:
            errors.append(f"{c.channel}: {e}")
            continue
        for d in clips:
            views = d.get("view_count") or d.get("views") or 0
            created = d.get("created_at", "")
            category = (d.get("category") or {}).get("name", "")
            if cfg.game and category and category.lower() != cfg.game.lower():
                continue
            if views < cfg.min_views or not created or age_hours(created, now) > cfg.lookback_hours:
                continue
            out.append(Clip(
                id=f"kick:{d['id']}", platform="kick", creator=c.channel, credit=c.credit,
                title=d.get("title", ""), url=f"https://kick.com/{c.channel}?clip={d['id']}",
                views=views, created_at=created, duration=d.get("duration", 0),
                score=velocity(views, created, now),
            ))
    if errors and not out:
        raise RuntimeError("; ".join(errors))
    for e in errors:
        print(f"  kick: {e}")
    return out
