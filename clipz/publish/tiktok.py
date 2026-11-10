"""Post a rendered clip to TikTok with the Content Posting API (direct post, file upload).

UNVERIFIED against the live API - written from TikTok's published spec, not yet run end to end.
Until TikTok has audited your developer app, it only accepts privacy level SELF_ONLY
(the post lands on your profile as private and you flip it to public in the TikTok app).
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

from ..config import Config
from ..db import Clip

NAME = "tiktok"
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
API = "https://open.tiktokapis.com/v2"
SCOPES = "user.info.basic,video.publish"
MAX_SINGLE_CHUNK = 64 * 1024 * 1024
CHUNK = 10 * 1024 * 1024


def _token_path(cfg: Config) -> Path:
    return cfg.data_dir / "tiktok_token.json"


def connected(cfg: Config) -> bool:
    return _token_path(cfg).exists()


def disconnect(cfg: Config) -> None:
    _token_path(cfg).unlink(missing_ok=True)


def _keys(cfg: Config) -> tuple[str, str]:
    key, secret = cfg.tiktok.get("client_key"), cfg.tiktok.get("client_secret")
    if not key or not secret:
        raise RuntimeError("enter your TikTok client key and secret first")
    return key, secret


def authorize_url(cfg: Config, redirect_uri: str) -> tuple[str, str, str]:
    """Returns (url to send the user to, state, code_verifier)."""
    key, _ = _keys(cfg)
    state, verifier = secrets.token_urlsafe(16), secrets.token_urlsafe(48)
    # TikTok's desktop flow wants the PKCE challenge as hex SHA-256, not the usual base64url.
    challenge = hashlib.sha256(verifier.encode()).hexdigest()
    query = urlencode({
        "client_key": key, "scope": SCOPES, "response_type": "code", "redirect_uri": redirect_uri,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    })
    return f"{AUTH_URL}?{query}", state, verifier


def _token_request(cfg: Config, **fields) -> dict:
    key, secret = _keys(cfg)
    r = requests.post(f"{API}/oauth/token/", data={"client_key": key, "client_secret": secret, **fields},
                      timeout=30)
    data = r.json()
    if "access_token" not in data:
        raise RuntimeError(f"TikTok login failed: {data.get('error_description') or data}")
    data["expires_at"] = time.time() + int(data.get("expires_in", 0))
    _token_path(cfg).write_text(json.dumps(data))
    return data


def finish_connect(cfg: Config, code: str, verifier: str, redirect_uri: str) -> None:
    _token_request(cfg, code=code, grant_type="authorization_code",
                   redirect_uri=redirect_uri, code_verifier=verifier)


def _access_token(cfg: Config) -> str:
    if not connected(cfg):
        raise RuntimeError("TikTok is not connected")
    tok = json.loads(_token_path(cfg).read_text())
    if tok["expires_at"] < time.time() + 60:
        tok = _token_request(cfg, grant_type="refresh_token", refresh_token=tok["refresh_token"])
    return tok["access_token"]


def chunk_plan(size: int) -> tuple[int, int]:
    """(chunk_size, total_chunk_count). Small files go up whole; otherwise fixed chunks with
    the remainder folded into the last one, as TikTok requires."""
    if size <= MAX_SINGLE_CHUNK:
        return size, 1
    return CHUNK, size // CHUNK


def caption(clip: Clip, hashtags: list[str]) -> str:
    tags = " ".join(f"#{t.lstrip('#')}" for t in hashtags)
    return f"{clip.title.strip()} | clip: {clip.credit} {tags}".strip()[:2200]


def upload(cfg: Config, clip: Clip) -> str:
    headers = {"Authorization": f"Bearer {_access_token(cfg)}",
               "Content-Type": "application/json; charset=UTF-8"}
    path = Path(clip.out_path)
    size = path.stat().st_size
    chunk_size, count = chunk_plan(size)
    r = requests.post(f"{API}/post/publish/video/init/", headers=headers, timeout=60, json={
        "post_info": {"title": caption(clip, cfg.render.get("hashtags", [])),
                      "privacy_level": cfg.tiktok.get("privacy", "SELF_ONLY")},
        "source_info": {"source": "FILE_UPLOAD", "video_size": size,
                        "chunk_size": chunk_size, "total_chunk_count": count},
    })
    body = r.json()
    if body.get("error", {}).get("code", "ok") != "ok":
        raise RuntimeError(f"TikTok rejected the post: {body['error'].get('message') or body['error']['code']}")
    upload_url, publish_id = body["data"]["upload_url"], body["data"]["publish_id"]
    with path.open("rb") as f:
        for i in range(count):
            start = i * chunk_size
            data = f.read(size - start if i == count - 1 else chunk_size)
            put = requests.put(upload_url, data=data, timeout=600, headers={
                "Content-Type": "video/mp4",
                "Content-Range": f"bytes {start}-{start + len(data) - 1}/{size}",
            })
            put.raise_for_status()
    return f"tiktok publish id {publish_id}"
