from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PLATFORMS = ("twitch", "kick", "youtube")


@dataclass
class Creator:
    platform: str
    channel: str
    credit: str = ""

    def __post_init__(self):
        if self.platform not in PLATFORMS:
            raise ValueError(f"creator platform must be one of {PLATFORMS}, got {self.platform!r}")
        if not self.credit:
            self.credit = self.channel


@dataclass
class Config:
    game: str = "Grand Theft Auto VI"
    keywords: list[str] = field(default_factory=lambda: ["gta"])
    lookback_hours: int = 24
    min_views: int = 50
    max_clips_per_run: int = 10
    max_duration: int = 60
    auto_approve: bool = False
    data_dir: Path = Path("data")
    creators: list[Creator] = field(default_factory=list)
    twitch: dict = field(default_factory=dict)
    render: dict = field(default_factory=dict)
    youtube_upload: dict = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "clipz.sqlite"


def load(path: str | Path = "config.toml") -> Config:
    path = Path(path)
    if not path.exists():
        raise SystemExit(f"{path} not found - copy config.example.toml to {path} and fill it in")
    raw = tomllib.loads(path.read_text())
    creators = [Creator(**c) for c in raw.pop("creators", [])]
    data_dir = Path(raw.pop("data_dir", "data"))
    cfg = Config(creators=creators, data_dir=data_dir, **raw)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg
