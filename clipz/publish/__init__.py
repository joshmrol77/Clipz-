from __future__ import annotations

from ..config import Config
from . import instagram, tiktok, youtube

PLATFORMS = {youtube.NAME: youtube, tiktok.NAME: tiktok, instagram.NAME: instagram}


def connected(cfg: Config) -> list[str]:
    return [name for name, mod in PLATFORMS.items() if mod.connected(cfg)]
