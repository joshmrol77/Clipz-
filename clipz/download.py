from __future__ import annotations

from pathlib import Path

import yt_dlp
from yt_dlp.utils import download_range_func

from .db import Clip


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
