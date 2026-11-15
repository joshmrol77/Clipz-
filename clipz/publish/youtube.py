"""Upload a rendered clip to YouTube Shorts with the official Data API.

Connecting sends you to Google to authorise your channel (OAuth "Web application" client);
Google then sends you back to the app, which keeps a refresh token so posting keeps working.
Plain HTTP requests rather than Google's client library, which is too big for a cloud function.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

from . import tokens
from ..config import Config
from ..db import Clip

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
NAME = "youtube"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"


def metadata(clip: Clip, hashtags: list[str], gaming: bool = True) -> dict:
    tags = " ".join(f"#{t.lstrip('#')}" for t in ["Shorts", *hashtags])
    title = clip.title.strip()
    room = 100 - len(" #Shorts")  # YouTube titles max out at 100 chars
    if len(title) > room:
        title = title[: room - 1].rstrip() + "…"
    return {
        "title": f"{title} #Shorts",
        "description": f"Clip credit: {clip.credit}\nOriginal: {clip.url}\n\n{tags}",
        "tags": [t.lstrip("#") for t in hashtags],
        "categoryId": "20" if gaming else "24",  # Gaming / Entertainment
    }


def connected(cfg: Config) -> bool:
    return tokens.load(cfg, NAME) is not None


def disconnect(cfg: Config) -> None:
    tokens.delete(cfg, NAME)


def _client(cfg: Config) -> tuple[str, str]:
    yt = cfg.youtube_upload
    if not (yt.get("client_id") and yt.get("client_secret")):
        raise RuntimeError("enter your Google OAuth client ID and secret first")
    return yt["client_id"], yt["client_secret"]


def authorize_url(cfg: Config, redirect_uri: str) -> tuple[str, str, str]:
    """Returns (url to send the user to, state, code_verifier)."""
    client_id, _ = _client(cfg)
    state, verifier = secrets.token_urlsafe(16), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    query = urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": " ".join(SCOPES), "state": state,
        # offline + consent so Google gives us a refresh token and posting keeps working unattended
        "access_type": "offline", "prompt": "consent",
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    return f"{AUTH_URL}?{query}", state, verifier


def _token_request(cfg: Config, **fields) -> dict:
    client_id, client_secret = _client(cfg)
    r = requests.post(TOKEN_URL, timeout=30, data={"client_id": client_id, "client_secret": client_secret, **fields})
    data = r.json()
    if "access_token" not in data:
        raise RuntimeError(f"Google sign-in failed: {data.get('error_description') or data.get('error') or data}")
    return data


def finish_connect(cfg: Config, code: str, verifier: str, redirect_uri: str) -> None:
    data = _token_request(cfg, code=code, code_verifier=verifier, redirect_uri=redirect_uri,
                          grant_type="authorization_code")
    if "refresh_token" not in data:
        raise RuntimeError("Google didn't grant lasting access - disconnect Clipz in your Google "
                           "account's security settings, then connect again")
    data["expires_at"] = time.time() + int(data.get("expires_in", 0))
    tokens.save(cfg, NAME, data)


def _access_token(cfg: Config) -> str:
    tok = tokens.load(cfg, NAME)
    if not tok:
        raise RuntimeError("YouTube is not connected - connect it in the app's Accounts tab")
    if tok.get("expires_at", 0) < time.time() + 60:
        fresh = _token_request(cfg, grant_type="refresh_token", refresh_token=tok["refresh_token"])
        tok.update(access_token=fresh["access_token"], expires_at=time.time() + int(fresh.get("expires_in", 0)))
        tokens.save(cfg, NAME, tok)
    return tok["access_token"]


def upload(cfg: Config, clip: Clip) -> str:
    """Resumable upload: announce the video with its details, then send the file in one go."""
    path = Path(clip.out_path)
    size = path.stat().st_size
    auth = {"Authorization": f"Bearer {_access_token(cfg)}"}
    start = requests.post(
        UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status"}, timeout=60,
        headers={**auth, "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)},
        json={
            "snippet": metadata(clip, cfg.hashtags_for(clip), cfg.is_gaming(clip)),
            "status": {"privacyStatus": cfg.youtube_upload.get("privacy", "private"),
                       "selfDeclaredMadeForKids": False},
        })
    if start.status_code != 200 or "Location" not in start.headers:
        raise RuntimeError(f"YouTube refused the upload: {_error(start)}")
    with path.open("rb") as f:
        done = requests.put(start.headers["Location"], data=f, timeout=900,
                            headers={**auth, "Content-Type": "video/mp4", "Content-Length": str(size)})
    if done.status_code not in (200, 201):
        raise RuntimeError(f"YouTube upload failed: {_error(done)}")
    return f"https://youtube.com/shorts/{done.json()['id']}"


def _error(r: requests.Response) -> str:
    try:
        return r.json()["error"]["message"]
    except Exception:
        return f"HTTP {r.status_code}"
