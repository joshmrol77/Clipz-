"""Everything Clipz remembers, in one database: SQLite on your own computer, Postgres when it
runs in the cloud (set DATABASE_URL). SQL is written once, SQLite-style, and translated.

Clip status flow:

discovered -> queued -> rendered -> approved -> published
     \\-> skipped             \\-> rejected          (any step can also -> failed)

`discovered` clips are what the Browse screen shows. `queued` means "download/edit this clip
next" - set when you pick one in Browse, for the top new finds (if auto-edit is on), re-edits
and retries. `skipped` is a find you passed on in Browse.
`posts` records each (clip, social platform) upload so a clip is never posted twice to the
same place and a failure on one platform can be retried without re-posting to the others.
`kv` holds settings, account sign-ins and work requests; `locks` makes sure only one worker
runs at a time; `log` is the Activity feed.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
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
    src_path: str = ""      # downloaded footage: a local path, or a cloud storage URL
    out_path: str = ""      # finished video: a local path, or a cloud storage URL
    published_url: str = ""
    error: str = ""
    layout: str = ""        # "" = use the default layout from settings
    background: str = ""    # gameplay video name, for the split layout
    thumbnail: str = ""     # picture shown in Browse
    preview: str = ""       # what Browse plays: an embeddable page, or a video file URL
    uploader: str = ""      # who posted it, when that isn't the streamer (another clipper)
    category: str = ""      # set for clips found through a followed game
    game: str = ""          # the game/category being played, when the platform says
    approve: str = ""       # "1" = approve as soon as it's edited
    autopost: str = ""      # "1" = and post it to the connected accounts straight away
    seen: str = ""          # "1" once it has been shown in the feed
    language: str = ""      # e.g. "en", when the platform says
    heat: float = 0.0       # how many times faster than a typical clip from the same streamer/game it's gaining views


COLUMNS = list(Clip.__dataclass_fields__)
PG_TYPES = {"views": "BIGINT", "duration": "DOUBLE PRECISION", "start": "DOUBLE PRECISION",
            "end": "DOUBLE PRECISION", "score": "DOUBLE PRECISION", "heat": "DOUBLE PRECISION"}
LOG_KEEP = 300


def _q(name: str) -> str:
    return f'"{name}"'  # quote column names: "start" and "end" are SQL keywords


class _Postgres:
    """Makes a psycopg connection accept SQLite-style SQL (`?` and `:name` placeholders)."""
    dialect = "postgres"

    def __init__(self, url: str):
        import psycopg
        from psycopg.rows import dict_row
        self._con = psycopg.connect(url, autocommit=True, row_factory=dict_row)

    @staticmethod
    def _translate(sql: str) -> str:
        sql = re.sub(r"(?<![:\w]):([A-Za-z_]\w*)", r"%(\1)s", sql)
        return sql.replace("?", "%s")

    def execute(self, sql: str, params=()):
        return self._con.execute(self._translate(sql), params)

    def commit(self) -> None:
        pass  # autocommit

    def close(self) -> None:
        self._con.close()


def connect(target: str | Path):
    """target: a Postgres URL, or a path to a SQLite file."""
    if str(target).startswith(("postgres://", "postgresql://")):
        con = _Postgres(str(target))
        cols = ", ".join(f"{_q(c)} {'TEXT PRIMARY KEY' if c == 'id' else PG_TYPES.get(c, 'TEXT')}" for c in COLUMNS)
        con.execute(f"CREATE TABLE IF NOT EXISTS clips ({cols})")
        for c in COLUMNS:  # tables made by an older version gain new columns here
            con.execute(f"ALTER TABLE clips ADD COLUMN IF NOT EXISTS {_q(c)} {PG_TYPES.get(c, 'TEXT')} DEFAULT NULL")
        con.execute("CREATE TABLE IF NOT EXISTS log (id BIGSERIAL PRIMARY KEY, at DOUBLE PRECISION, msg TEXT)")
    else:
        con = sqlite3.connect(target, check_same_thread=False, timeout=30)
        con.row_factory = sqlite3.Row
        cols = ", ".join(f"{_q(c)} PRIMARY KEY" if c == "id" else _q(c) for c in COLUMNS)
        con.execute(f"CREATE TABLE IF NOT EXISTS clips ({cols})")
        have = {r["name"] for r in con.execute("PRAGMA table_info(clips)")}
        for c in COLUMNS:
            if c not in have:
                con.execute(f"ALTER TABLE clips ADD COLUMN {_q(c)} DEFAULT ''")
        con.execute("CREATE TABLE IF NOT EXISTS log (id INTEGER PRIMARY KEY AUTOINCREMENT, at, msg)")
    con.execute("CREATE TABLE IF NOT EXISTS posts (clip_id TEXT, platform TEXT, ref TEXT, error TEXT, "
                "PRIMARY KEY (clip_id, platform))")
    con.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS locks (name TEXT PRIMARY KEY, holder TEXT, label TEXT, "
                "expires DOUBLE PRECISION)")
    con.commit()
    return con


def credit_line(clip: Clip) -> str:
    """Who to credit in a post: the streamer, plus whoever posted the clip if that was someone else."""
    return f"{clip.credit} (clip via {clip.uploader})" if clip.uploader else clip.credit


def _clip(row) -> Clip:
    data = dict(row)
    for c in ("src_path", "out_path", "published_url", "error", "layout", "background",
              "thumbnail", "preview", "uploader", "category", "game", "approve", "seen", "autopost", "language"):
        data[c] = data.get(c) or ""
    try:
        data["heat"] = float(data.get("heat") or 0)
    except (TypeError, ValueError):
        data["heat"] = 0.0
    return Clip(**{c: data[c] for c in COLUMNS})


# ---- clips ----------------------------------------------------------------------------------

def add(con, clip: Clip) -> bool:
    """Insert a newly discovered clip. Returns False if we have already seen it - in which
    case, if it is still waiting to be processed, its views and score are refreshed so a clip
    that blows up after we first saw it moves to the front of the queue."""
    cols = ", ".join(_q(c) for c in COLUMNS)
    marks = ", ".join(f":{c}" for c in COLUMNS)
    cur = con.execute(f"INSERT INTO clips ({cols}) VALUES ({marks}) ON CONFLICT (id) DO NOTHING", asdict(clip))
    added = cur.rowcount == 1
    if not added:
        con.execute("UPDATE clips SET views = :views, score = :score, heat = :heat "
                    "WHERE id = :id AND status = 'discovered'", asdict(clip))
    con.commit()
    return added


def update(con, clip_id: str, **fields) -> None:
    sets = ", ".join(f"{_q(k)} = :{k}" for k in fields)
    cur = con.execute(f"UPDATE clips SET {sets} WHERE id = :id", {**fields, "id": clip_id})
    con.commit()
    if cur.rowcount == 0:
        raise KeyError(clip_id)


def get(con, clip_id: str) -> Clip | None:
    row = con.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
    return _clip(row) if row else None


def by_status(con, status: str, limit: int | None = None) -> list[Clip]:
    q = "SELECT * FROM clips WHERE status = ? ORDER BY score DESC, id"
    if limit:
        q += f" LIMIT {int(limit)}"
    return [_clip(r) for r in con.execute(q, (status,))]


def all_clips(con) -> list[Clip]:
    return [_clip(r) for r in con.execute("SELECT * FROM clips ORDER BY score DESC, id")]


def prune(con, before_iso: str) -> int:
    """Forget finds you never picked that are older than `before_iso` (keeps Browse fresh)."""
    cur = con.execute("DELETE FROM clips WHERE status IN ('discovered', 'skipped') AND created_at < ?",
                      (before_iso,))
    con.commit()
    return cur.rowcount


def record_post(con, clip_id: str, platform: str, ref: str = "", error: str = "") -> None:
    con.execute("INSERT INTO posts VALUES (?, ?, ?, ?) ON CONFLICT (clip_id, platform) "
                "DO UPDATE SET ref = excluded.ref, error = excluded.error", (clip_id, platform, ref, error))
    con.commit()


def posts(con, clip_id: str) -> dict[str, dict]:
    rows = con.execute("SELECT platform, ref, error FROM posts WHERE clip_id = ?", (clip_id,))
    return {r["platform"]: {"ref": r["ref"], "error": r["error"]} for r in rows}


# ---- key/value: settings, sign-ins, work requests --------------------------------------------

def kv_get(con, key: str, default=None):
    row = con.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def kv_set(con, key: str, value) -> None:
    con.execute("INSERT INTO kv VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)))
    con.commit()


def kv_delete(con, key: str) -> None:
    con.execute("DELETE FROM kv WHERE key = ?", (key,))
    con.commit()


# ---- one worker at a time ---------------------------------------------------------------------

def lock_acquire(con, name: str, holder: str, label: str, ttl: float) -> bool:
    """Take the lock if it is free or its holder has gone quiet for longer than its ttl."""
    now = time.time()
    row = con.execute(
        "INSERT INTO locks VALUES (?, ?, ?, ?) ON CONFLICT (name) DO UPDATE SET "
        "holder = excluded.holder, label = excluded.label, expires = excluded.expires "
        "WHERE locks.expires < ? OR locks.holder = excluded.holder RETURNING holder",
        (name, holder, label, now + ttl, now)).fetchone()
    con.commit()
    return bool(row) and row["holder"] == holder


def lock_refresh(con, name: str, holder: str, label: str, ttl: float) -> None:
    con.execute("UPDATE locks SET label = ?, expires = ? WHERE name = ? AND holder = ?",
                (label, time.time() + ttl, name, holder))
    con.commit()


def lock_release(con, name: str, holder: str) -> None:
    con.execute("DELETE FROM locks WHERE name = ? AND holder = ?", (name, holder))
    con.commit()


def lock_label(con, name: str) -> str:
    """What the current worker is doing, or "" if nothing is running."""
    row = con.execute("SELECT label FROM locks WHERE name = ? AND expires >= ?", (name, time.time())).fetchone()
    return row["label"] if row else ""


# ---- Activity feed ----------------------------------------------------------------------------

def log_append(con, msg: str) -> None:
    cur = con.execute("INSERT INTO log (at, msg) VALUES (?, ?) RETURNING id", (time.time(), msg))
    newest = cur.fetchone()["id"]
    con.execute("DELETE FROM log WHERE id <= ?", (newest - LOG_KEEP,))
    con.commit()


def log_tail(con, n: int = 60) -> list[tuple[float, str]]:
    """Newest n entries, oldest first, as (unix time, message)."""
    rows = con.execute("SELECT at, msg FROM log ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    return [(float(r["at"]), r["msg"]) for r in reversed(rows)]
