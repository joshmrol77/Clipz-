"""Kick. EXPERIMENTAL: Kick has no documented public API for clips or channel search, so this
uses the same JSON endpoints the website does. Cloudflare can block them or they can change
without notice; when that happens, type the channel name exactly, or add clips by hand with
`clipz add <url>`.

Kick clips are made by viewers, so a channel's clips already include other people's clipping.
"""
from __future__ import annotations

from datetime import datetime, timezone

import requests

from ..config import Config, Creator
from ..db import Clip
from ..rank import age_hours, velocity

HEADERS = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}


def _get(url: str, **params) -> dict:
    r = requests.get(url, params=params, headers=HEADERS, timeout=20)
    if r.status_code == 403:
        raise RuntimeError("Kick blocked the request (it sometimes refuses servers)")
    r.raise_for_status()
    return r.json()


def search_channels(cfg: Config, query: str, limit: int = 8) -> list[dict]:
    query = query.strip()
    if len(query) < 2:
        return []
    data = _get("https://kick.com/api/search", searched_word=query)
    channels = data.get("channels", []) if isinstance(data, dict) else []
    out = []
    for ch in channels:
        user = ch.get("user") or {}
        slug = ch.get("slug") or user.get("username", "").lower()
        if not slug:
            continue
        out.append({
            "login": slug, "name": user.get("username") or slug,
            "image": user.get("profile_pic") or user.get("profilepic") or "",
            "live": bool(ch.get("is_live") or ch.get("isLive")),
            "game": f"{ch.get('followers_count', ch.get('followersCount', 0)):,} followers"
                    if ch.get("followers_count", ch.get("followersCount")) is not None else "",
        })
    out.sort(key=lambda r: (not r["live"], r["login"] != query.lower()))
    return out[:limit]


def discover(cfg: Config, creators: list[Creator]) -> list[Clip]:
    now = datetime.now(timezone.utc)
    out, errors = [], []
    for c in creators:
        try:
            clips = _get(f"https://kick.com/api/v2/channels/{c.channel}/clips", sort="view", time="day").get("clips", [])
        except Exception as e:
            errors.append(f"{c.channel}: {e}")
            continue
        for d in clips:
            views = d.get("view_count") or d.get("views") or 0
            created = d.get("created_at", "")
            category = (d.get("category") or {}).get("name", "")
            if cfg.game and not c.all_content and category and category.lower() != cfg.game.lower():
                continue
            if views < cfg.min_views or not created or age_hours(created, now) > cfg.lookback_hours:
                continue
            clipper = (d.get("creator") or {}).get("username", "")
            out.append(Clip(
                id=f"kick:{d['id']}", platform="kick", creator=c.channel, credit=c.credit,
                title=d.get("title", ""), url=f"https://kick.com/{c.channel}?clip={d['id']}",
                views=views, created_at=created, duration=d.get("duration", 0),
                score=velocity(views, created, now),
                thumbnail=d.get("thumbnail_url", ""),
                preview=d.get("video_url") or d.get("clip_url") or "",
                uploader=clipper if clipper.lower() != c.channel.lower() else "",
            ))
    if errors and not out:
        raise RuntimeError("; ".join(errors))
    for e in errors:
        print(f"  kick: {e}")
    return out
