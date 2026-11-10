"""Post a rendered clip to Instagram as a Reel (Instagram API with Instagram Login).

UNVERIFIED against the live API - written from Meta's published spec, not yet run end to end.
Needs an Instagram Business or Creator account. Connecting = pasting the access token that
Meta's developer dashboard generates for your account; it lasts 60 days and is renewed
automatically whenever Clipz posts.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

from ..config import Config
from ..db import Clip

NAME = "instagram"
GRAPH = "https://graph.instagram.com/v21.0"
UPLOAD = "https://rupload.facebook.com/ig-api-upload/v21.0"
RENEW_AFTER = 7 * 24 * 3600        # renew a token once it is a week old
PROCESSING_TIMEOUT = 300


def _token_path(cfg: Config) -> Path:
    return cfg.data_dir / "instagram_token.json"


def connected(cfg: Config) -> bool:
    return _token_path(cfg).exists()


def disconnect(cfg: Config) -> None:
    _token_path(cfg).unlink(missing_ok=True)


def _check(r: requests.Response) -> dict:
    try:
        data = r.json()
    except ValueError:
        r.raise_for_status()
        raise RuntimeError(f"Instagram sent an unreadable reply ({r.status_code})")
    if "error" in data or r.status_code >= 400:
        err = data.get("error", {})
        raise RuntimeError(f"Instagram: {err.get('message') or data.get('debug_info', {}).get('message') or data}")
    return data


def connect(cfg: Config, token: str) -> str:
    """Check the pasted token works and remember it. Returns the account's username."""
    token = token.strip()
    if not token:
        raise RuntimeError("paste your Instagram access token first")
    me = _check(requests.get(f"{GRAPH}/me", params={"fields": "user_id,username", "access_token": token}, timeout=30))
    _token_path(cfg).write_text(json.dumps({
        "access_token": token, "user_id": str(me.get("user_id") or me["id"]),
        "username": me.get("username", ""), "saved_at": time.time(),
    }))
    return me.get("username", "")


def _account(cfg: Config) -> dict:
    if not connected(cfg):
        raise RuntimeError("Instagram is not connected")
    acct = json.loads(_token_path(cfg).read_text())
    if time.time() - acct["saved_at"] > RENEW_AFTER:
        try:
            fresh = _check(requests.get("https://graph.instagram.com/refresh_access_token", timeout=30, params={
                "grant_type": "ig_refresh_token", "access_token": acct["access_token"]}))
            acct.update(access_token=fresh["access_token"], saved_at=time.time())
            _token_path(cfg).write_text(json.dumps(acct))
        except Exception:
            pass  # keep using the current token; if it has truly expired the post fails with a clear error
    return acct


def caption(clip: Clip, hashtags: list[str]) -> str:
    tags = " ".join(f"#{t.lstrip('#')}" for t in hashtags)
    return f"{clip.title.strip()}\n\nClip credit: {clip.credit}\n\n{tags}".strip()[:2200]


def upload(cfg: Config, clip: Clip, sleep=time.sleep) -> str:
    acct = _account(cfg)
    token, user = acct["access_token"], acct["user_id"]
    path = Path(clip.out_path)

    # 1. Open a container for the Reel, 2. send the file into it, 3. wait for Instagram to
    # process it, 4. publish.
    container = _check(requests.post(f"{GRAPH}/{user}/media", timeout=60, data={
        "media_type": "REELS", "upload_type": "resumable", "share_to_feed": "true",
        "caption": caption(clip, cfg.hashtags_for(clip)), "access_token": token,
    }))["id"]
    with path.open("rb") as f:
        _check(requests.post(f"{UPLOAD}/{container}", data=f, timeout=600, headers={
            "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(path.stat().st_size),
        }))
    deadline = time.time() + PROCESSING_TIMEOUT
    while True:
        status = _check(requests.get(f"{GRAPH}/{container}", timeout=30, params={
            "fields": "status_code,status", "access_token": token}))
        if status.get("status_code") == "FINISHED":
            break
        if status.get("status_code") in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram could not process the video: {status.get('status') or status['status_code']}")
        if time.time() > deadline:
            raise RuntimeError("Instagram is still processing the video - try posting again in a few minutes")
        sleep(5)
    media = _check(requests.post(f"{GRAPH}/{user}/media_publish", timeout=60, data={
        "creation_id": container, "access_token": token}))["id"]
    try:
        link = _check(requests.get(f"{GRAPH}/{media}", timeout=30, params={
            "fields": "permalink", "access_token": token})).get("permalink")
    except Exception:
        link = None
    return link or f"instagram media id {media}"
