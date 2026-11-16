from __future__ import annotations

from pathlib import Path

import yt_dlp
from yt_dlp.utils import download_range_func

from .db import Clip
from .render import ffmpeg


def stream_url(url: str, max_height: int = 720) -> str:
    """The direct address of a clip's video file (an mp4), without downloading it."""
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    formats = [f for f in info.get("formats") or [] if f.get("url") and f.get("vcodec") != "none"
               and (f.get("protocol") or "").startswith("http") and (f.get("height") or 0) <= max_height]
    if not formats:
        if info.get("url"):
            return info["url"]
        raise RuntimeError("no playable video found")
    return max(formats, key=lambda f: ((f.get("height") or 0), f.get("tbr") or 0))["url"]


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


def download(clip: Clip, dest_dir: Path) -> Path:
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
    }
    if clip.start is not None and clip.end is not None:
        opts["download_ranges"] = download_range_func(None, [(clip.start, clip.end)])
        opts["force_keyframes_at_cuts"] = True
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([clip.url])
    path = dest_dir / f"{stem}.mp4"
    if not path.exists():
        raise RuntimeError(f"download produced no file for {clip.url}")
    return path
