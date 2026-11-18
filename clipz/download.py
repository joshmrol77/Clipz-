from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

import yt_dlp
from yt_dlp.utils import download_range_func

from .db import Clip
from .render import ffmpeg


COOKIE_DOMAINS = ("youtube.com", "google.com", "youtu.be")


def clean_cookies(text: str) -> str:
    """Turn a pasted cookie export into a Netscape cookies.txt with only the YouTube/Google
    cookies in it. Accepts a cookies.txt file or the JSON list most cookie extensions export.
    Raises ValueError if there are no YouTube sign-in cookies in it."""
    text = (text or "").strip()
    rows = []
    if text.startswith("["):
        try:
            items = json.loads(text)
        except ValueError:
            raise ValueError("That doesn't look like a cookie export.")
        for c in items if isinstance(items, list) else []:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            domain = c.get("domain", "")
            expires = int(float(c.get("expirationDate") or c.get("expires") or 0))
            rows.append([domain, "TRUE" if domain.startswith(".") else "FALSE", c.get("path") or "/",
                         "TRUE" if c.get("secure") else "FALSE", str(max(expires, 0)), c["name"], str(c.get("value", ""))])
    else:
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            elif not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                rows.append(parts[:6] + ["\t".join(parts[6:])])
    rows = [r for r in rows if any(r[0].lstrip(".").endswith(d) for d in COOKIE_DOMAINS)]
    names = {r[5] for r in rows}
    if not rows:
        raise ValueError("No YouTube cookies found. Export them while signed in on youtube.com.")
    if not names & {"SID", "__Secure-1PSID", "__Secure-3PSID", "LOGIN_INFO", "SAPISID"}:
        raise ValueError("These cookies aren't signed in. Sign in on youtube.com first, then export again.")
    return "# Netscape HTTP Cookie File\n" + "\n".join("\t".join(r) for r in rows) + "\n"


@contextmanager
def _cookie_file(cookies: str):
    """yt-dlp reads (and rewrites) a cookies file, so hand it a private temporary copy."""
    if not cookies:
        yield None
        return
    fd, path = tempfile.mkstemp(suffix=".txt")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(cookies)
        yield path
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _with_cookies(opts: dict, path: str | None) -> dict:
    return {**opts, "cookiefile": path} if path else opts


def stream_url(url: str, max_height: int = 720, cookies: str = "") -> str:
    """The direct address of a clip's video file (an mp4), without downloading it."""
    with _cookie_file(cookies) as path, \
            yt_dlp.YoutubeDL(_with_cookies({"quiet": True, "no_warnings": True, "skip_download": True}, path)) as ydl:
        info = ydl.extract_info(url, download=False)
    formats = [f for f in info.get("formats") or [] if f.get("url") and f.get("vcodec") != "none"
               and (f.get("protocol") or "").startswith("http") and (f.get("height") or 0) <= max_height]
    if not formats:
        if info.get("url"):
            return info["url"]
        raise RuntimeError("no playable video found")
    return max(formats, key=lambda f: ((f.get("height") or 0), f.get("tbr") or 0))["url"]


LINK_PLATFORMS = {"youtube": "youtube", "youtubetab": "youtube", "twitch": "twitch", "kick": "kick",
                  "tiktok": "tiktok", "instagram": "instagram", "twitter": "x", "x": "x", "reddit": "reddit"}


def link_info(url: str, cookies: str = "") -> dict:
    """What a pasted link points to, without downloading the video: a dict for a new Clip."""
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
    with _cookie_file(cookies) as path, yt_dlp.YoutubeDL(_with_cookies(opts, path)) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("_type") == "playlist":
        entries = [e for e in info.get("entries") or [] if e]
        if not entries:
            raise RuntimeError("no video found at that link")
        info = entries[0]
    key = (info.get("extractor_key") or info.get("extractor") or "").lower().split(":")[0]
    platform = next((v for k, v in LINK_PLATFORMS.items() if key.startswith(k)), key or "link")
    if platform == "tiktok":
        handle = info.get("uploader") or info.get("channel")
    elif platform in ("x", "instagram"):
        handle = info.get("uploader_id") or info.get("channel") or info.get("uploader")
    else:
        handle = info.get("uploader_id") or info.get("channel") or info.get("uploader") or info.get("creator")
    handle = str(handle or "").lstrip("@") or "unknown"
    title = (info.get("title") or info.get("description") or "").strip().split("\n")[0][:200]
    if platform in ("x", "instagram", "tiktok") and title.lower().startswith(("video by", "post by")):
        title = ""
    created = ""
    if info.get("timestamp"):
        from datetime import datetime, timezone
        created = datetime.fromtimestamp(info["timestamp"], timezone.utc).isoformat()
    return {"id": f"link:{platform}:{info.get('id') or abs(hash(url))}", "platform": platform, "creator": handle,
            "credit": "", "title": title, "url": info.get("webpage_url") or url,
            "views": int(info.get("view_count") or 0), "created_at": created,
            "duration": float(info.get("duration") or 0), "thumbnail": info.get("thumbnail") or ""}


def fetch_video(url: str, dest_dir: Path, name: str, max_seconds: int = 180) -> Path:
    """Download the first `max_seconds` of any video link yt-dlp understands (or a direct
    .mp4 link) - used for gameplay backgrounds."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(name).stem
    opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True, "overwrites": True,
        "outtmpl": str(dest_dir / f"{stem}.%(ext)s"),
        "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
        "merge_output_format": "mp4", "ffmpeg_location": ffmpeg(),
        "download_ranges": download_range_func(None, [(0, max_seconds)]),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    path = dest_dir / f"{stem}.mp4"
    if not path.exists():
        raise RuntimeError("couldn't download a video from that link")
    return path


def download(clip: Clip, dest_dir: Path, cookies: str = "", max_seconds: int = 0) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = clip.id.replace(":", "_")
    opts = {
        "quiet": True,
        "no_warnings": True,
        "outtmpl": str(dest_dir / f"{stem}.%(ext)s"),
        "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
        "merge_output_format": "mp4",
        "overwrites": True,
        "ffmpeg_location": ffmpeg(),
        "socket_timeout": 30,
    }
    if clip.start is not None and clip.end is not None:
        opts["download_ranges"] = download_range_func(None, [(clip.start, clip.end)])
        opts["force_keyframes_at_cuts"] = True
    elif max_seconds and (not clip.duration or clip.duration > max_seconds + 30) and clip.platform not in ("twitch", "kick"):
        # a long video: only the part that ends up in the edit
        opts["download_ranges"] = download_range_func(None, [(0, max_seconds + 5)])
    with _cookie_file(cookies) as cookie_path, yt_dlp.YoutubeDL(_with_cookies(opts, cookie_path)) as ydl:
        ydl.download([clip.url])
    path = dest_dir / f"{stem}.mp4"
    if not path.exists():
        raise RuntimeError(f"download produced no file for {clip.url}")
    return path
