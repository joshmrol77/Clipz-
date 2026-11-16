"""Kick. Kick has no documented public API for clips, so this uses the endpoints Kick's own
website uses (the same ones yt-dlp uses): channel info at kick.com/api/v2, clip lists at
web.kick.com/api/v1. Kick sits behind bot protection that turns away ordinary scripts, so
requests go through curl_cffi, which presents itself like a real Chrome browser.

Kick clips are made by viewers, so a channel's clips already include other people's clipping.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from ..config import Config, Creator
from ..db import Clip
from ..rank import age_hours, velocity

HEADERS = {"Accept": "application/json"}


def _get(url: str, **params) -> dict:
    try:
        from curl_cffi import requests as browser
        r = browser.get(url, params=params or None, headers=HEADERS, impersonate="chrome", timeout=20)
    except ImportError:  # still works where Kick doesn't check, just less often
        import requests as browser
        r = browser.get(url, params=params or None, headers={**HEADERS, "User-Agent": "Mozilla/5.0"}, timeout=20)
    if r.status_code in (403, 429):
        raise RuntimeError(f"Kick refused the request ({r.status_code})")
    if r.status_code == 404:
        raise LookupError("not found on Kick")
    r.raise_for_status()
    return r.json()


def _time_range(lookback_hours: int) -> str:
    return "day" if lookback_hours <= 24 else "week" if lookback_hours <= 168 else "month" if lookback_hours <= 744 else "all"


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def channel(slug: str) -> dict:
    """{"id", "slug", "name", "image", "live"} for a channel, or LookupError."""
    d = _get(f"https://kick.com/api/v2/channels/{slug}")
    user = d.get("user") or {}
    return {"id": d.get("id"), "slug": d.get("slug") or slug, "name": user.get("username") or slug,
            "image": user.get("profile_pic") or "", "live": bool(d.get("livestream"))}


def search_channels(cfg: Config, query: str, limit: int = 8) -> list[dict]:
    query = query.strip()
    if len(query) < 2:
        return []
    out = []
    try:
        data = _get("https://kick.com/api/search", searched_word=query)
        for ch in (data.get("channels", []) if isinstance(data, dict) else []):
            user = ch.get("user") or {}
            slug = ch.get("slug") or user.get("username", "").lower()
            if slug:
                followers = ch.get("followers_count", ch.get("followersCount"))
                out.append({"login": slug, "name": user.get("username") or slug,
                            "image": user.get("profile_pic") or user.get("profilepic") or "",
                            "live": bool(ch.get("is_live") or ch.get("isLive")),
                            "game": f"{followers:,} followers" if isinstance(followers, int) else ""})
    except Exception:
        pass
    if not any(r["login"] == slugify(query) for r in out):
        try:  # the exact name always works, even when Kick's search doesn't
            ch = channel(slugify(query))
            out.insert(0, {"login": ch["slug"], "name": ch["name"], "image": ch["image"], "live": ch["live"], "game": ""})
        except Exception:
            pass
    out.sort(key=lambda r: (not r["live"], r["login"] != query.lower()))
    return out[:limit]


def search_categories(query: str) -> list[dict]:
    """Kick categories matching a game name: [{"slug", "name"}]."""
    try:
        data = _get("https://kick.com/api/search", searched_word=query)
        return [{"slug": c["slug"], "name": c.get("name", c["slug"])}
                for c in (data.get("categories", []) if isinstance(data, dict) else []) if c.get("slug")]
    except Exception:
        return []


def _clip(cfg: Config, d: dict, now, creator: str, credit: str, category: str = "", game_labels: dict | None = None):
    views = d.get("views_count") or d.get("view_count") or d.get("views") or 0
    created = d.get("created_at", "")
    if views < cfg.min_views or not created or age_hours(created, now) > cfg.lookback_hours:
        return None
    ch = d.get("channel") or {}
    slug = ch.get("slug") or creator
    clipper = (d.get("creator") or {}).get("username", "")
    game = (d.get("category") or {}).get("name", "")
    return Clip(
        id=f"kick:{d['id']}", platform="kick", creator=creator or slug, credit=credit or f"kick.com/{slug}",
        title=d.get("title", ""), url=f"https://kick.com/{slug}/clips/{d['id']}",
        views=views, created_at=created, duration=d.get("duration") or 0, score=velocity(views, created, now),
        thumbnail=d.get("thumbnail_url", ""),
        preview=d.get("playback_url") or d.get("clip_url") or d.get("video_url") or "",
        uploader=clipper if clipper and clipper.lower() != slug.lower() else "",
        category=category, game=(game_labels or {}).get(game.lower(), game),
        language=d.get("language") or (d.get("livestream") or {}).get("language") or "",
    )


def _channel_clips(channel_id, time_range: str, pages: int = 2) -> list[dict]:
    clips, query = [], {"sort": "views", "time": time_range}
    for _ in range(pages):
        data = (_get(f"https://web.kick.com/api/v1/channels/{channel_id}/clips", **query) or {}).get("data") or {}
        clips += data.get("clips") or []
        if not data.get("cursor"):
            break
        query["cursor"] = data["cursor"]
    return clips


def _labels(cfg: Config) -> dict:
    return {g["name"].lower(): g.get("label") or g["name"] for g in cfg.games}


def discover(cfg: Config, creators: list[Creator]) -> list[Clip]:
    now = datetime.now(timezone.utc)
    out, errors = [], []
    for c in creators:
        try:
            ch = channel(c.channel)
            raw = _channel_clips(ch["id"], _time_range(cfg.lookback_hours))
            print(f"  kick: {c.channel}: Kick sent {len(raw)} clips", flush=True)
            for d in raw:
                game = (d.get("category") or {}).get("name", "")
                if cfg.game and not c.all_content and game and game.lower() != cfg.game.lower():
                    continue
                clip = _clip(cfg, d, now, c.channel, c.credit, game_labels=_labels(cfg))
                if clip:
                    out.append(clip)
        except Exception as e:
            errors.append(f"{c.channel}: {e}")
    if errors and not out:
        raise RuntimeError("; ".join(errors))
    for e in errors:
        print(f"  kick: {e}")
    return out


# Kick's website shows a Clips tab on category pages; these are the addresses it may use.
CATEGORY_CLIP_URLS = (
    "https://web.kick.com/api/v1/categories/{slug}/clips",
    "https://kick.com/api/v2/categories/{slug}/clips",
)


def discover_games(cfg: Config, games: list[dict]) -> list[Clip]:
    """The most-viewed Kick clips in each followed game's category."""
    now = datetime.now(timezone.utc)
    out, errors = [], []
    for g in games:
        slug = g.get("kick_slug") or slugify(g["name"])
        label = g.get("label") or g["name"]
        clips, last_error = None, None
        time_range = _time_range(cfg.lookback_hours)
        attempts = [(url, params) for url in CATEGORY_CLIP_URLS for params in (
            {"sort": "views", "time": time_range}, {"sort": "view", "time": time_range}, {"sort": "views"}, {})]
        for url, params in attempts:
            try:
                data = _get(url.format(slug=slug), **params)
                clips = (data.get("data") or {}).get("clips") if "data" in data else data.get("clips")
                if clips is not None:
                    print(f"  kick games: {label} clips from {url.format(slug=slug)} {params}")
                    break
            except Exception as e:
                last_error = e
        if clips is None:
            errors.append(f"{label}: {last_error or 'no clips list'}")
            continue
        print(f"  kick games: {label}: Kick sent {len(clips)} clips", flush=True)
        for d in clips:
            ch = d.get("channel") or {}
            clip = _clip(cfg, d, now, ch.get("slug", ""), "", label, {g["name"].lower(): label})
            if clip:
                clip.game = label
                out.append(clip)
    if errors and not out:
        raise RuntimeError("; ".join(errors))
    for e in errors:
        print(f"  kick games: {e}")
    return out
