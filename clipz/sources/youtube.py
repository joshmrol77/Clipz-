"""YouTube. There is no clips API, so for each streamer Clipz looks in two places:

1. Their own channel (if you added them by their YouTube channel): recent uploads, cutting out
   the "most replayed" section of each (the heatmap on the player's seek bar).
2. Everyone else: a YouTube search for the streamer's name, newest first, keeping short videos -
   these are usually other clippers' uploads. They're credited to the streamer and the uploader.

Searches use YouTube's own search page through yt-dlp, so no API key is needed. YouTube
sometimes refuses servers it thinks are bots; when it does, the error shows in Activity.
"""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote_plus

import base64
from urllib.parse import quote

import yt_dlp

from ..config import Config, Creator
from ..db import Clip
from ..rank import age_hours, best_window, velocity

RECENT_VIDEOS = 4
WINDOW = 45.0                 # seconds cut from a long video's most-replayed part
MAX_THIRD_PARTY_LENGTH = 180  # longer uploads aren't clips
SEARCH_RESULTS = 40
CHANNELS = "EgIQAg%3D%3D"     # YouTube search filter ("sp"): channels only


def videos_filter(lookback_hours: int) -> str:
    """The "sp" search filter for: videos only, uploaded within the window, most viewed first.
    It's a tiny protobuf: sort (3 = view count), then upload date (2 today, 3 this week,
    4 this month, 5 this year) and type (1 = video)."""
    window = 2 if lookback_hours <= 24 else 3 if lookback_hours <= 168 else 4 if lookback_hours <= 744 else 5
    return quote(base64.b64encode(bytes([0x08, 3, 0x12, 0x04, 0x08, window, 0x10, 0x01])).decode(), safe="")
QUIET = {"quiet": True, "no_warnings": True, "skip_download": True}


def _channel_url(channel: str) -> str:
    if channel.startswith("http"):
        return channel.rstrip("/")
    return f"https://www.youtube.com/{channel if channel.startswith('@') else '@' + channel}"


def _search(query: str, sp: str, limit: int) -> list[dict]:
    url = f"https://www.youtube.com/results?search_query={quote_plus(query)}&sp={sp}"
    with yt_dlp.YoutubeDL({**QUIET, "extract_flat": True, "playlistend": limit}) as ydl:
        return [e for e in (ydl.extract_info(url, download=False).get("entries") or []) if e]


def _thumb(entry: dict) -> str:
    thumbs = entry.get("thumbnails") or []
    if not thumbs:
        return entry.get("thumbnail") or ""
    url = thumbs[-1].get("url", "")
    return "https:" + url if url.startswith("//") else url


def search_channels(cfg: Config, query: str, limit: int = 8) -> list[dict]:
    query = query.strip()
    if len(query) < 3:
        return []
    out = []
    for e in _search(query, CHANNELS, limit):
        handle = e.get("uploader_id") or ""
        out.append({
            "login": handle if handle.startswith("@") else (e.get("channel_url") or e.get("url") or ""),
            "name": e.get("channel") or e.get("title") or handle,
            "image": _thumb(e), "live": False,
            "game": f"{e['channel_follower_count']:,} subscribers" if e.get("channel_follower_count") else (handle or ""),
        })
    return [r for r in out if r["login"]]


def _embed(video_id: str, start: float | None = None, end: float | None = None) -> str:
    url = f"https://www.youtube.com/embed/{video_id}?playsinline=1"
    if start is not None:
        url += f"&start={int(start)}&end={int(end)}"
    return url


def _own_uploads(cfg: Config, c: Creator, now) -> list[Clip]:
    """Their recent uploads and finished livestreams (the channel's Live tab)."""
    out, entries = [], []
    for tab in ("/videos", "/streams"):
        try:
            with yt_dlp.YoutubeDL({**QUIET, "extract_flat": True, "playlistend": RECENT_VIDEOS}) as ydl:
                entries += ydl.extract_info(_channel_url(c.channel) + tab, download=False).get("entries") or []
        except Exception:
            continue  # not every channel has both tabs
    for entry in entries:
        title = entry.get("title") or ""
        if entry.get("timestamp") and age_hours(datetime.fromtimestamp(entry["timestamp"], timezone.utc).isoformat(), now) > cfg.lookback_hours + 24:
            continue  # clearly too old: skip the slow full lookup
        if entry.get("live_status") == "is_live":
            continue  # still live: no replay graph yet
        if cfg.keywords and not c.all_content and not any(k.lower() in title.lower() for k in cfg.keywords):
            continue
        with yt_dlp.YoutubeDL(QUIET) as ydl:
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
            duration=end - start, start=start, end=end, score=velocity(views, created, now) * peak,
            thumbnail=info.get("thumbnail") or "", preview=_embed(info["id"], start, end),
        ))
    return out


def _short_videos(cfg: Config, query: str, now, must_mention: str = "") -> list[dict]:
    """Most-viewed short videos for a search, within the time window. Searching with #shorts
    brings up Shorts (clips) instead of long videos."""
    results = _search(f"{query} #shorts", videos_filter(cfg.lookback_hours), SEARCH_RESULTS)
    seen, out = set(), []
    results = [e for e in results if e.get("id") and not (e["id"] in seen or seen.add(e["id"]))]
    for e in results:
        # YouTube's search page lists Shorts without a length (and sometimes without views):
        # they're at most a minute, so don't throw them away for that.
        # Regular videos always come with a length; a missing one means a Short.
        duration = e.get("duration") or 60
        if not e.get("id") or not duration or duration > MAX_THIRD_PARTY_LENGTH:
            continue
        e = {**e, "duration": duration}
        title = (e.get("title") or "").lower()
        if must_mention and must_mention.lower() not in title and must_mention.lower().replace(" ", "") not in title.replace(" ", ""):
            continue  # search results drift; keep ones that actually name the subject
        ts = e.get("timestamp")
        created = datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else now.isoformat()
        views = e.get("view_count")
        if (views is not None and views < cfg.min_views) or age_hours(created, now) > cfg.lookback_hours:
            continue
        out.append({**e, "created": created})
    print(f"  youtube search {query!r}: {len(results)} results, {len(out)} kept", flush=True)
    return out


def _as_clip(cfg: Config, e: dict, creator: str, credit: str, now, uploader: str, category: str = "") -> Clip:
    duration, length = e["duration"], min(e["duration"], cfg.max_duration)
    cut = duration > cfg.max_duration
    views = e.get("view_count") or 0
    return Clip(
        id=f"youtube:{e['id']}", platform="youtube", creator=creator, credit=credit,
        title=e.get("title") or "", url=e.get("url") or f"https://www.youtube.com/watch?v={e['id']}",
        views=views, created_at=e["created"], duration=length,
        start=0.0 if cut else None, end=float(length) if cut else None,
        score=velocity(views, e["created"], now), thumbnail=_thumb(e), preview=_embed(e["id"]),
        uploader=uploader, category=category,
    )


def _others_uploads(cfg: Config, c: Creator, now) -> list[Clip]:
    """The most-viewed short videos about this streamer, from any channel."""
    name = c.name or c.channel.lstrip("@").rsplit("/", 1)[-1]
    query = name if c.all_content or not cfg.game else f"{name} {cfg.game}"
    own_ids = {c.channel.lower().lstrip("@"), name.lower()}
    out = []
    for e in _short_videos(cfg, query, now, must_mention=name):
        uploader = e.get("channel") or e.get("uploader") or ""
        handle = (e.get("uploader_id") or "").lstrip("@").lower()
        theirs = handle in own_ids or uploader.lower() in own_ids
        out.append(_as_clip(cfg, e, c.channel, c.credit, now, "" if theirs else uploader))
    return out


def discover_games(cfg: Config, games: list[dict]) -> list[Clip]:
    """The most-viewed short videos about each followed game, from any channel."""
    now = datetime.now(timezone.utc)
    out = []
    for g in games:
        label = g.get("label") or g["name"]
        for e in _short_videos(cfg, label, now):
            uploader = e.get("channel") or e.get("uploader") or ""
            handle = e.get("uploader_id") or ""
            credit = f"youtube.com/{handle}" if handle.startswith("@") else (e.get("channel_url") or "YouTube")
            clip = _as_clip(cfg, e, uploader, credit, now, "", label)
            clip.game = label
            out.append(clip)
    return out


def discover(cfg: Config, creators: list[Creator]) -> list[Clip]:
    now = datetime.now(timezone.utc)
    out, errors = [], []
    for c in creators:
        for finder in (_own_uploads, _others_uploads):
            try:
                out += finder(cfg, c, now)
            except Exception as e:
                errors.append(f"{c.channel}: {str(e)[:150]}")
    if errors and not out:
        raise RuntimeError("; ".join(errors))
    for e in errors:
        print(f"  youtube: {e}")
    seen, unique = set(), []
    for clip in out:            # the same video can turn up in both places
        if clip.id not in seen:
            seen.add(clip.id)
            unique.append(clip)
    return unique
