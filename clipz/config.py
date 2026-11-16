"""Settings come from three layers, later ones winning:
defaults  <-  config.toml (optional, hand-edited)  <-  settings saved by the app (in the database)

Where things are kept:
- database: Postgres when DATABASE_URL (or POSTGRES_URL) is set, else <data_dir>/clipz.sqlite
- data_dir: working files. On Vercel only /tmp is writable, so it defaults to /tmp/clipz there.
"""
from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import db

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
    category: str = ""            # your own grouping, e.g. "GTA 6" or "IRL"
    name: str = ""                # display name; also what Clipz searches YouTube for

    def __post_init__(self):
        self.channel = self.channel.strip()
        self.hashtags = [h.strip().lstrip("#") for h in self.hashtags if h.strip()]
        self.category = self.category.strip() or "General"
        self.name = self.name.strip()
        if self.platform not in PLATFORMS:
            raise ValueError(f"platform must be one of {PLATFORMS}, got {self.platform!r}")
        if not self.channel:
            raise ValueError("channel is required")
        if not self.credit:
            handle = self.channel.rstrip("/").rsplit("/", 1)[-1]
            site = {"twitch": "twitch.tv/", "kick": "kick.com/", "youtube": "youtube.com/"}[self.platform]
            if self.platform == "youtube" and handle.startswith("UC") and len(handle) == 24:
                handle = "channel/" + handle          # a channel ID rather than an @handle
            elif self.platform == "youtube" and not handle.startswith("@"):
                handle = "@" + handle
            self.credit = site + handle


@dataclass
class Config:
    game: str = "Grand Theft Auto VI"
    keywords: list[str] = field(default_factory=lambda: ["gta"])
    lookback_hours: int = 168         # only consider clips from the last this-many hours
    min_views: int = 50
    max_clips_per_run: int = 0        # auto-edit this many of the best finds per search; 0 = pick in Browse
    max_duration: int = 60
    auto_approve: bool = False
    schedule_minutes: int = 0          # app only: 0 = never run automatically
    data_dir: Path = Path("data")
    creators: list[Creator] = field(default_factory=list)
    # Followed games: [{"name": Twitch category name, "twitch_id", "image", "label": what you call it}]
    games: list[dict] = field(default_factory=list)
    # Hidden from the feed: {"creators": ["platform:name"], "games": ["name"], "labels": {value: shown name}}
    muted: dict = field(default_factory=dict)
    twitch: dict = field(default_factory=dict)
    render: dict = field(default_factory=dict)
    youtube_upload: dict = field(default_factory=dict)
    tiktok: dict = field(default_factory=dict)
    database: str = ""                 # Postgres URL or SQLite path; filled in by load()
    _con: object = field(default=None, repr=False, compare=False)

    def connect(self):
        """The database connection for this Config (opened once, then reused)."""
        if self._con is None:
            self._con = db.connect(self.database or self.data_dir / "clipz.sqlite")
        return self._con

    def creator_for(self, clip) -> Creator | None:
        for c in self.creators:
            if c.platform == clip.platform and c.channel == clip.creator:
                return c
        return None

    def hashtags_for(self, clip) -> list[str]:
        creator = self.creator_for(clip)
        return (creator.hashtags if creator and creator.hashtags else self.render.get("hashtags", []))

    def game_aliases(self) -> list[tuple[str, str]]:
        """(lowercase phrase to look for in titles, followed game's label), longest first."""
        romans = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10"}
        out = []
        for g in self.games:
            label = g.get("label") or g["name"]
            names = {label.lower(), g["name"].lower()}
            for n in list(names):
                words = n.split()
                if words and words[-1] in romans:          # "grand theft auto vi" -> "... 6"
                    names.add(" ".join(words[:-1] + [romans[words[-1]]]))
            for n in list(names):
                words = n.split()
                if len(words) >= 3:                         # "grand theft auto 6" -> "gta 6"
                    initials = "".join(w[0] for w in words[:-1])
                    names.add(f"{initials} {words[-1]}")
                    names.add(f"{initials}{words[-1]}")
            out += [(n, label) for n in names if len(n) >= 3]
        return sorted(out, key=lambda x: -len(x[0]))

    def infer_game(self, title: str) -> str:
        import re
        t = " " + re.sub(r"[^a-z0-9]+", " ", title.lower()) + " "
        for phrase, label in self.game_aliases():
            if f" {re.sub(r'[^a-z0-9]+', ' ', phrase).strip()} " in t:
                return label
        return ""

    def category_of(self, clip) -> str:
        if clip.category:
            return clip.category
        creator = self.creator_for(clip)
        return creator.category if creator else "General"

    def is_gaming(self, clip) -> bool:
        creator = self.creator_for(clip)
        return bool(self.game) and not (creator and creator.all_content)

    @property
    def db_path(self):
        return self.database or self.data_dir / "clipz.sqlite"


def database_url() -> str:
    return os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL") or ""


def default_data_dir() -> str:
    return os.environ.get("CLIPZ_DATA_DIR") or ("/tmp/clipz" if os.environ.get("VERCEL") else "data")


def _merge(into: dict, changes: dict) -> dict:
    """Sections merge key by key; everything else replaces."""
    for key, value in changes.items():
        if key in SECTIONS:
            into[key] = {**into.get(key, {}), **value}
        else:
            into[key] = value
    return into


def load(path: str | Path = "config.toml") -> Config:
    path = Path(path)
    raw = tomllib.loads(path.read_text()) if path.exists() else {}
    data_dir = Path(raw.pop("data_dir", default_data_dir()))
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg = Config(data_dir=data_dir, database=database_url())
    con = cfg.connect()
    saved = db.kv_get(con, "settings")
    legacy = data_dir / "settings.json"   # settings saved by an older version
    if saved is None and legacy.exists():
        saved = json.loads(legacy.read_text())
        db.kv_set(con, "settings", saved)
    _merge(raw, saved or {})
    creators = [Creator(**c) for c in raw.pop("creators", [])]
    cfg = Config(creators=creators, data_dir=data_dir, database=cfg.database, **raw)
    cfg._con = con
    return cfg


def save_settings(cfg: Config, changes: dict) -> None:
    """Merge `changes` into the settings saved by the app."""
    con = cfg.connect()
    db.kv_set(con, "settings", _merge(db.kv_get(con, "settings", {}), changes))
