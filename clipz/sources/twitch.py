"""Twitch clips via the official Helix API (app access token)."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import requests

from .. import db
from ..config import Config, Creator
from ..db import Clip
from ..rank import velocity

API = "https://api.twitch.tv/helix"


def app_token(client_id: str, client_secret: str) -> str:
    """Exchange the app's keys for an access token. Also how we check the keys are right."""
    try:
        r = requests.post(
            "https://id.twitch.tv/oauth2/token",
            data={"client_id": client_id, "client_secret": client_secret, "grant_type": "client_credentials"},
            timeout=30,
        )
    except requests.RequestException:
        raise RuntimeError("Couldn't reach Twitch - check your connection and try again in a minute")
    if r.status_code in (400, 401, 403):
        raise RuntimeError("Twitch didn't accept that Client ID and secret - check them on "
                           "dev.twitch.tv/console/apps (make a New Secret if unsure)")
    r.raise_for_status()
    return r.json()["access_token"]


def _session(cfg: Config) -> requests.Session:
    cid, secret = cfg.twitch.get("client_id"), cfg.twitch.get("client_secret")
    if not cid or not secret:
        raise RuntimeError("Twitch isn't connected - connect it in Settings")
    # App tokens last about two months; reuse one rather than asking Twitch on every search.
    con = cfg.connect()
    cached = db.kv_get(con, "twitch_app_token") or {}
    if cached.get("client_id") != cid or cached.get("expires_at", 0) < time.time():
        cached = {"client_id": cid, "token": app_token(cid, secret), "expires_at": time.time() + 24 * 3600}
        db.kv_set(con, "twitch_app_token", cached)
    s = requests.Session()
    s.headers.update({"Client-Id": cid, "Authorization": f"Bearer {cached['token']}"})
    return s


def search_channels(cfg: Config, query: str, limit: int = 8) -> list[dict]:
    """Channels whose name matches what's been typed so far, live ones first."""
    query = query.strip()
    if len(query) < 2:
        return []
    found = _get(_session(cfg), "search/channels", query=query, first=20)
    found.sort(key=lambda d: (not d.get("is_live"), d["broadcaster_login"] != query.lower()))
    return [{
        "login": d["broadcaster_login"], "name": d["display_name"], "image": d.get("thumbnail_url", ""),
        "live": bool(d.get("is_live")), "game": d.get("game_name", ""),
    } for d in found[:limit]]


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
