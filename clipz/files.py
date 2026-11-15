"""Where videos live. On your own computer: files in the data folder, and a clip's `src_path` /
`out_path` is a file path. In the cloud (a Vercel Blob store connected to the project): files are uploaded to Vercel Blob and those fields hold the file's URL.
Blob URLs carry a long random suffix, so they can't be guessed; anything Clipz edits is meant
to be posted publicly anyway. Sign-ins and keys are never stored here - they're in the database.
"""
from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from urllib.parse import urlparse

import requests

from .config import Config
from . import db


def _adopt(suffix: str, target: str, looks_like: str) -> bool:
    """Vercel names storage settings with a prefix chosen when the store is connected (BLOB by
    default). Copy a custom-prefixed one to the name the Vercel SDK reads."""
    if os.environ.get(target):
        return True
    for key, value in os.environ.items():
        if key.endswith(suffix) and value.strip().startswith(looks_like):
            os.environ[target] = value.strip()
            return True
    return False


def cloud() -> bool:
    """Is a Vercel Blob store connected? Newer connections provide a store ID and sign in with
    Vercel's built-in identity (OIDC); older ones provide a read-write key."""
    if os.environ.get("VERCEL_BLOB_READ_WRITE_TOKEN"):
        return True
    return (_adopt("_READ_WRITE_TOKEN", "BLOB_READ_WRITE_TOKEN", "vercel_blob_rw_")
            or _adopt("_STORE_ID", "BLOB_STORE_ID", "store_"))


def is_url(ref: str) -> bool:
    return ref.startswith(("https://", "http://"))


def save(cfg: Config, local: Path, folder: str) -> str:
    """Keep `local` for later and return the reference to store in the database."""
    if not cloud():
        return str(local)
    from vercel.blob import sync as blob

    data = Path(local).read_bytes()
    result = blob.put_blob(f"{folder}/{Path(local).name}", data, access="public",
                           add_random_suffix=True, content_type="video/mp4")
    return result.url


def local(cfg: Config, ref: str) -> Path:
    """A file on this machine for `ref`, downloading it first if it lives in cloud storage.
    The path may not exist if a local file was deleted - callers check."""
    if not is_url(ref):
        return Path(ref)
    cache = cfg.data_dir / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha1(ref.encode()).hexdigest()[:16] + Path(urlparse(ref).path).suffix
    path = cache / name
    if not path.exists():
        with requests.get(ref, stream=True, timeout=120) as r:
            r.raise_for_status()
            tmp = path.with_suffix(".part")
            with tmp.open("wb") as f:
                shutil.copyfileobj(r.raw, f)
            tmp.rename(path)
    return path


def remove(cfg: Config, ref: str) -> None:
    """Delete a stored file we no longer need (e.g. the old version of a re-edited clip)."""
    if not ref or not is_url(ref) or not cloud():
        return
    from vercel.blob import sync as blob
    try:
        blob.delete_blob(ref)
    except Exception:
        pass  # leftover storage is harmless; don't fail the edit over it


def tidy(*paths: Path | None) -> None:
    """In the cloud the local disk is small and temporary: drop working copies once uploaded."""
    if not cloud():
        return
    for p in paths:
        if p:
            Path(p).unlink(missing_ok=True)


# ---- gameplay videos for the split layout ----------------------------------------------------

def backgrounds(cfg: Config) -> dict[str, str]:
    """name -> reference. Videos dropped by hand into <data_dir>/backgrounds count too."""
    from .render import BACKGROUND_TYPES

    found = dict(db.kv_get(cfg.connect(), "backgrounds", {}))
    folder = cfg.data_dir / "backgrounds"
    if folder.is_dir():
        for p in folder.iterdir():
            if p.suffix.lower() in BACKGROUND_TYPES and p.name not in found:
                found[p.name] = str(p)
    return dict(sorted(found.items()))


def add_background(cfg: Config, path: Path, name: str) -> None:
    ref = save(cfg, path, "backgrounds")
    con = cfg.connect()
    current = db.kv_get(con, "backgrounds", {})
    remove(cfg, current.get(name, ""))
    current[name] = ref
    db.kv_set(con, "backgrounds", current)
    if is_url(ref):
        tidy(path)
