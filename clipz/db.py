"""SQLite queue of clips. Status flow:

discovered -> downloaded -> rendered -> approved -> published
                                   \\-> rejected          (any step can also -> failed)
"""
from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Clip:
    id: str                 # "<platform>:<platform clip id>"
    platform: str
    creator: str            # channel as written in config
    credit: str
    title: str
    url: str
    views: int = 0
    created_at: str = ""    # ISO 8601 UTC
    duration: float = 0.0
    start: float | None = None   # for cutting a section out of a longer video
    end: float | None = None
    score: float = 0.0
    status: str = "discovered"
    src_path: str = ""
    out_path: str = ""
    published_url: str = ""
    error: str = ""


COLUMNS = list(Clip.__dataclass_fields__)


def connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    cols = ", ".join(f"{c} PRIMARY KEY" if c == "id" else c for c in COLUMNS)
    con.execute(f"CREATE TABLE IF NOT EXISTS clips ({cols})")
    return con


def add(con: sqlite3.Connection, clip: Clip) -> bool:
    """Insert a newly discovered clip. Returns False if we have already seen it."""
    marks = ", ".join(f":{c}" for c in COLUMNS)
    cur = con.execute(f"INSERT OR IGNORE INTO clips VALUES ({marks})", asdict(clip))
    con.commit()
    return cur.rowcount == 1


def update(con: sqlite3.Connection, clip_id: str, **fields) -> None:
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    cur = con.execute(f"UPDATE clips SET {sets} WHERE id = :id", {**fields, "id": clip_id})
    con.commit()
    if cur.rowcount == 0:
        raise KeyError(clip_id)


def get(con: sqlite3.Connection, clip_id: str) -> Clip | None:
    row = con.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
    return Clip(**row) if row else None


def by_status(con: sqlite3.Connection, status: str, limit: int | None = None) -> list[Clip]:
    q = "SELECT * FROM clips WHERE status = ? ORDER BY score DESC"
    if limit:
        q += f" LIMIT {int(limit)}"
    return [Clip(**r) for r in con.execute(q, (status,))]


def all_clips(con: sqlite3.Connection) -> list[Clip]:
    return [Clip(**r) for r in con.execute("SELECT * FROM clips ORDER BY score DESC")]
