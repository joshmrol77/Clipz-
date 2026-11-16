from __future__ import annotations

from ..config import Config
from ..db import Clip
from . import kick, twitch, youtube

_SOURCES = {"twitch": twitch, "kick": kick, "youtube": youtube}
_GAME_SOURCES = {"twitch": twitch, "youtube": youtube}   # Kick has no category clips feed we can use


def discover(cfg: Config, log=print) -> list[Clip]:
    """Collect candidate clips from every streamer you follow, then from every game you follow.
    Streamers go first, so a clip found both ways keeps the streamer's category.
    One failing source doesn't stop the others."""
    jobs = [(platform, module.discover, [c for c in cfg.creators if c.platform == platform])
            for platform, module in _SOURCES.items()]
    jobs += [(f"{platform} games", module.discover_games, cfg.games) for platform, module in _GAME_SOURCES.items()]
    found: list[Clip] = []
    for label, finder, items in jobs:
        if not items or (label == "twitch games" and not cfg.twitch.get("client_id")):
            continue
        try:
            clips = finder(cfg, items)
        except Exception as e:  # network / API errors: report and keep going
            log(f"  {label}: FAILED - {str(e)[:200]}")
            continue
        log(f"  {label}: {len(clips)} candidate(s)")
        found += clips
    return found
