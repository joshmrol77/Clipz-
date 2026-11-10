"""Settings come from three layers, later ones winning:
defaults  <-  config.toml (optional, hand-edited)  <-  <data_dir>/settings.json (written by the app)
"""
from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PLATFORMS = ("twitch", "kick", "youtube")
SECTIONS = ("twitch", "render", "youtube_upload", "tiktok")
SCALARS = {
    "game": str, "lookback_hours": int, "min_views": int, "max_clips_per_run": int,
    "max_duration": int, "auto_approve": bool, "schedule_minutes": int,
}


@dataclass
class Creator:
    platform: str
    channel: str
    credit: str = ""
    all_content: bool = False     # True = clip everything they stream, not just the configured game
    hashtags: list[str] = field(default_factory=list)   # empty = use the global hashtags

    def __post_init__(self):
        self.channel = self.channel.strip()
        self.hashtags = [h.strip().lstrip("#") for h in self.hashtags if h.strip()]
        if self.platform not in PLATFORMS:
            raise ValueError(f"platform must be one of {PLATFORMS}, got {self.platform!r}")
        if not self.channel:
            raise ValueError("channel is required")
        if not self.credit:
            handle = self.channel.rstrip("/").rsplit("/", 1)[-1]
            site = {"twitch": "twitch.tv/", "kick": "kick.com/", "youtube": "youtube.com/"}[self.platform]
            if self.platform == "youtube" and not handle.startswith("@"):
                handle = "@" + handle
            self.credit = site + handle


@dataclass
class Config:
    game: str = "Grand Theft Auto VI"
    keywords: list[str] = field(default_factory=lambda: ["gta"])
    lookback_hours: int = 24
    min_views: int = 50
    max_clips_per_run: int = 10
    max_duration: int = 60
    auto_approve: bool = False
    schedule_minutes: int = 0          # app only: 0 = never run automatically
    data_dir: Path = Path("data")
    creators: list[Creator] = field(default_factory=list)
    twitch: dict = field(default_factory=dict)
    render: dict = field(default_factory=dict)
    youtube_upload: dict = field(default_factory=dict)
    tiktok: dict = field(default_factory=dict)

    def creator_for(self, clip) -> Creator | None:
        for c in self.creators:
            if c.platform == clip.platform and c.channel == clip.creator:
                return c
        return None

    def hashtags_for(self, clip) -> list[str]:
        creator = self.creator_for(clip)
        return (creator.hashtags if creator and creator.hashtags else self.render.get("hashtags", []))

    def is_gaming(self, clip) -> bool:
        creator = self.creator_for(clip)
        return bool(self.game) and not (creator and creator.all_content)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "clipz.sqlite"

    @property
    def settings_path(self) -> Path:
        return self.data_dir / "settings.json"


def load(path: str | Path = "config.toml") -> Config:
    path = Path(path)
    raw = tomllib.loads(path.read_text()) if path.exists() else {}
    data_dir = Path(raw.pop("data_dir", "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    settings = data_dir / "settings.json"
    if settings.exists():
        for key, value in json.loads(settings.read_text()).items():
            if key in SECTIONS:
                raw[key] = {**raw.get(key, {}), **value}
            else:
                raw[key] = value
    creators = [Creator(**c) for c in raw.pop("creators", [])]
    return Config(creators=creators, data_dir=data_dir, **raw)


def save_settings(cfg: Config, changes: dict) -> None:
    """Merge `changes` into settings.json. Sections merge key by key; everything else replaces."""
    current = json.loads(cfg.settings_path.read_text()) if cfg.settings_path.exists() else {}
    for key, value in changes.items():
        if key in SECTIONS:
            current[key] = {**current.get(key, {}), **value}
        else:
            current[key] = value
    cfg.settings_path.write_text(json.dumps(current, indent=2))
