"""Account sign-ins, kept in the database so they survive restarts and cloud deployments."""
from __future__ import annotations

from .. import db
from ..config import Config


def load(cfg: Config, name: str) -> dict | None:
    return db.kv_get(cfg.connect(), f"token:{name}")


def save(cfg: Config, name: str, data: dict) -> None:
    db.kv_set(cfg.connect(), f"token:{name}", data)


def delete(cfg: Config, name: str) -> None:
    db.kv_delete(cfg.connect(), f"token:{name}")
