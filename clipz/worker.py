"""Background work, done one step at a time so it fits in short cloud function runs.

Buttons and the daily schedule don't do the work themselves: they record what's wanted
(`want:find`, `want:publish`, or a clip with status `queued`) and wake a worker. A worker takes
the lock, does steps until nothing is left or its time is up, and then - if work remains - wakes
the next worker. On your own computer the worker is a thread; on Vercel it's a request to
/api/worker. If a hand-off is ever lost, the app wakes a worker again the next time it's opened.
"""
from __future__ import annotations

import time
import uuid

from . import db, pipeline, publish
from .config import Config

LOCK = "worker"
TTL = 330          # seconds; longer than the longest cloud function run (300s)
WANT_FIND, WANT_PUBLISH = "want:find", "want:publish"


def request_find(cfg: Config) -> None:
    con = cfg.connect()
    db.kv_set(con, WANT_FIND, time.time())
    db.kv_set(con, "last_find", time.time())


def request_publish(cfg: Config) -> None:
    con = cfg.connect()
    if not db.kv_get(con, WANT_PUBLISH):
        db.kv_set(con, WANT_PUBLISH, {"tried": []})


def busy(cfg: Config) -> str:
    """What the worker is doing right now, or ""."""
    return db.lock_label(cfg.connect(), LOCK)


def pending(cfg: Config) -> bool:
    con = cfg.connect()
    return bool(db.kv_get(con, WANT_FIND) or db.by_status(con, "queued", 1) or db.kv_get(con, WANT_PUBLISH))


def run(cfg: Config, log, budget: float | None = None) -> str:
    """Do work until done ("done"), out of time ("more"), or another worker has it ("busy")."""
    con, me = cfg.connect(), uuid.uuid4().hex
    if not db.lock_acquire(con, LOCK, me, "Starting", TTL):
        return "busy"
    started = time.time()
    try:
        while True:
            def label(text: str) -> None:
                db.lock_refresh(con, LOCK, me, text, TTL)
            if not step(cfg, con, log, label):
                return "done"
            if budget is not None and time.time() - started > budget:
                return "more"
    finally:
        db.lock_release(con, LOCK, me)


def step(cfg: Config, con, log, label) -> bool:
    """Do the next piece of work. Returns False when there is nothing left."""
    if not db.kv_get(con, "migrated:no_auto_edit"):
        db.kv_set(con, "migrated:no_auto_edit", time.time())
        if (n := pipeline.return_auto_edits(con)):
            log(f"Put {n} automatically picked clip(s) back in the feed")
    if db.kv_get(con, WANT_FIND):
        db.kv_delete(con, WANT_FIND)
        label("Finding clips")
        try:
            pipeline.discover(cfg, con, log)
        except Exception as e:
            log(f"Finding clips FAILED: {e}")
        return True

    queued = db.by_status(con, "queued", 1)
    if queued:
        label("Editing clips")
        pipeline.process(cfg, con, queued, log)
        done = db.get(con, queued[0].id)
        if done.status == "approved" and (cfg.auto_approve or done.autopost == "1"):
            request_publish(cfg)
        return True

    want = db.kv_get(con, WANT_PUBLISH)
    if want:
        targets = publish.connected(cfg)
        # Each approved clip gets one attempt per "post" request, so a clip that keeps failing
        # on one platform can't loop forever; pressing Post again retries it.
        todo = [c for c in db.by_status(con, "approved") if c.id not in want["tried"]]
        if not targets or not todo:
            db.kv_delete(con, WANT_PUBLISH)
            log("No social accounts connected yet." if not targets else "Posting finished.")
            return True
        label("Posting")
        db.kv_set(con, WANT_PUBLISH, {"tried": [*want["tried"], todo[0].id]})
        pipeline.publish_clip(cfg, con, todo[0], targets, log)
        return True
    return False
