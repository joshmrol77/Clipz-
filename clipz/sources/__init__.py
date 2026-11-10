from __future__ import annotations

from ..config import Config
from ..db import Clip
from . import kick, twitch, youtube

_SOURCES = {"twitch": twitch, "kick": kick, "youtube": youtube}


def discover(cfg: Config, log=print) -> list[Clip]:
    """Collect candidate clips from every creator in the allowlist. One failing source
    doesn't stop the others."""
    found: list[Clip] = []
    for platform, module in _SOURCES.items():
        creators = [c for c in cfg.creators if c.platform == platform]
        if not creators:
            continue
        try:
            clips = module.discover(cfg, creators)
        except Exception as e:  # network / API errors: report and keep going
            log(f"  {platform}: FAILED - {e}")
            continue
        log(f"  {platform}: {len(clips)} candidate(s)")
        found += clips
    return found
