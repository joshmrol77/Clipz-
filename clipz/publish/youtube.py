"""Upload a rendered clip to YouTube Shorts with the official Data API (OAuth desktop flow).
Connecting sends you to Google to authorise your channel; the token is cached afterwards."""
from __future__ import annotations

from pathlib import Path

from ..config import Config
from ..db import Clip

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
NAME = "youtube"


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


def _libs():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import Flow
        from googleapiclient.discovery import build
    except ImportError:
        raise RuntimeError("YouTube upload needs extra packages: pip install -e .")
    return Request, Credentials, Flow, build


def _token_path(cfg: Config) -> Path:
    return cfg.data_dir / "youtube_token.json"


def connected(cfg: Config) -> bool:
    return _token_path(cfg).exists()


def disconnect(cfg: Config) -> None:
    _token_path(cfg).unlink(missing_ok=True)


def _flow(cfg: Config, redirect_uri: str):
    _, _, Flow, _ = _libs()
    yt = cfg.youtube_upload
    if not (yt.get("client_id") and yt.get("client_secret")):
        raise RuntimeError("enter your Google OAuth client ID and secret first")
    return Flow.from_client_config({"web": {
        "client_id": yt["client_id"], "client_secret": yt["client_secret"],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [redirect_uri],
    }}, SCOPES, redirect_uri=redirect_uri)


def authorize_url(cfg: Config, redirect_uri: str) -> tuple[str, str, str | None]:
    """Returns (url to send the user to, state, code_verifier)."""
    flow = _flow(cfg, redirect_uri)
    # offline + consent so Google gives us a refresh token and posting keeps working unattended
    url, state = flow.authorization_url(access_type="offline", prompt="consent")
    return url, state, getattr(flow, "code_verifier", None)


def finish_connect(cfg: Config, code: str, verifier: str | None, redirect_uri: str) -> None:
    flow = _flow(cfg, redirect_uri)
    flow.code_verifier = verifier
    flow.fetch_token(code=code)
    _token_path(cfg).write_text(flow.credentials.to_json())


def _service(cfg: Config):
    Request, Credentials, _, build = _libs()
    if not connected(cfg):
        raise RuntimeError("YouTube is not connected - connect it in the app's Accounts tab")
    creds = Credentials.from_authorized_user_file(_token_path(cfg), SCOPES)
    if not creds.valid:
        creds.refresh(Request())
        _token_path(cfg).write_text(creds.to_json())
    return build("youtube", "v3", credentials=creds)


def upload(cfg: Config, clip: Clip) -> str:
    from googleapiclient.http import MediaFileUpload

    body = {
        "snippet": metadata(clip, cfg.hashtags_for(clip), cfg.is_gaming(clip)),
        "status": {
            "privacyStatus": cfg.youtube_upload.get("privacy", "private"),
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(clip.out_path, chunksize=-1, resumable=True, mimetype="video/mp4")
    request = _service(cfg).videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        _, response = request.next_chunk()
    return f"https://youtube.com/shorts/{response['id']}"
