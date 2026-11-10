"""Twitch clips via the official Helix API (app access token)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

from ..config import Config, Creator
from ..db import Clip
from ..rank import velocity

API = "https://api.twitch.tv/helix"


def _session(cfg: Config) -> requests.Session:
    cid, secret = cfg.twitch.get("client_id"), cfg.twitch.get("client_secret")
    if not cid or not secret:
        raise RuntimeError("set [twitch] client_id and client_secret in config.toml")
    r = requests.post(
        "https://id.twitch.tv/oauth2/token",
        data={"client_id": cid, "client_secret": secret, "grant_type": "client_credentials"},
        timeout=30,
    )
    r.raise_for_status()
    s = requests.Session()
    s.headers.update({"Client-Id": cid, "Authorization": f"Bearer {r.json()['access_token']}"})
    return s


def _get(s: requests.Session, path: str, **params) -> list[dict]:
    r = s.get(f"{API}/{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()["data"]


def discover(cfg: Config, creators: list[Creator]) -> list[Clip]:
    s = _session(cfg)
    game_id = None
    if cfg.game and not all(c.all_content for c in creators):
        games = _get(s, "games", name=cfg.game)
        if not games:
            raise RuntimeError(f"Twitch has no category named {cfg.game!r}")
        game_id = games[0]["id"]

    users = _get(s, "users", login=[c.channel.lower() for c in creators])
    ids = {u["login"]: u["id"] for u in users}
    now = datetime.now(timezone.utc)
    started = (now - timedelta(hours=cfg.lookback_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    out = []
    for c in creators:
        uid = ids.get(c.channel.lower())
        if not uid:
            print(f"  twitch: channel {c.channel!r} not found, skipping")
            continue
        # Helix returns clips sorted by view count, so the first page is the top 100.
        for d in _get(s, "clips", broadcaster_id=uid, started_at=started,
                      ended_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"), first=100):
            if game_id and not c.all_content and d["game_id"] != game_id:
                continue
            if d["view_count"] < cfg.min_views:
                continue
            out.append(Clip(
                id=f"twitch:{d['id']}", platform="twitch", creator=c.channel, credit=c.credit,
                title=d["title"], url=d["url"], views=d["view_count"], created_at=d["created_at"],
                duration=d["duration"], score=velocity(d["view_count"], d["created_at"], now),
            ))
    return out
