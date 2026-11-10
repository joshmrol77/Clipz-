"""Upload a rendered clip to YouTube Shorts with the official Data API (OAuth desktop flow).
The first run opens a browser to authorise your channel; the token is cached afterwards."""
from __future__ import annotations

from pathlib import Path

from .config import Config
from .db import Clip

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
TOKEN_FILE = "youtube_token.json"


def metadata(clip: Clip, hashtags: list[str]) -> dict:
    tags = " ".join(f"#{t.lstrip('#')}" for t in ["Shorts", *hashtags])
    title = clip.title.strip()
    room = 100 - len(" #Shorts")  # YouTube titles max out at 100 chars
    if len(title) > room:
        title = title[: room - 1].rstrip() + "…"
    return {
        "title": f"{title} #Shorts",
        "description": f"Clip credit: {clip.credit}\nOriginal: {clip.url}\n\n{tags}",
        "tags": [t.lstrip("#") for t in hashtags],
        "categoryId": "20",  # Gaming
    }


def _service(cfg: Config):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        raise SystemExit("YouTube upload needs extra packages: pip install -e '.[youtube]'")
    token = Path(TOKEN_FILE)
    creds = Credentials.from_authorized_user_file(token, SCOPES) if token.exists() else None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            secrets = cfg.youtube_upload.get("client_secrets_file", "client_secret.json")
            creds = InstalledAppFlow.from_client_secrets_file(secrets, SCOPES).run_local_server(port=0)
        token.write_text(creds.to_json())
    return build("youtube", "v3", credentials=creds)


def upload(cfg: Config, clip: Clip) -> str:
    from googleapiclient.http import MediaFileUpload

    body = {
        "snippet": metadata(clip, cfg.render.get("hashtags", [])),
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
