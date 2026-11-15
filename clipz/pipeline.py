"""The steps shared by the command line, the app and the cloud worker."""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

from . import db, download as dl, files, publish, render as rn, sources
from .config import Config


def discover(cfg: Config, con, log=print) -> int:
    """Look for new clips and remember them. Returns how many were new."""
    if not cfg.creators:
        log("No streamers on your list yet - add some first.")
        return 0
    log("Looking for new clips...")
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    db.prune(con, week_ago)   # finds you never picked, from over a week ago
    new = sum(db.add(con, c) for c in sources.discover(cfg, log))
    log(f"{new} new clip(s) found - see Browse")
    return new


def queue_top(cfg: Config, con) -> list[str]:
    """Auto-edit: mark the best clips found so far as next in line to be downloaded and edited.
    max_clips_per_run = 0 means you pick every clip yourself in Browse."""
    if cfg.max_clips_per_run <= 0:
        return []
    ids = [c.id for c in db.by_status(con, "discovered", cfg.max_clips_per_run)]
    for clip_id in ids:
        db.update(con, clip_id, status="queued")
    return ids


def find_and_render(cfg: Config, con, log=print) -> None:
    """Everything at once - used by `clipz run`."""
    discover(cfg, con, log)
    process(cfg, con, db.by_status(con, "discovered", cfg.max_clips_per_run or 10), log)


def backgrounds(cfg: Config) -> list[str]:
    """Names of the gameplay videos available for the split layout."""
    return list(files.backgrounds(cfg))


def process(cfg: Config, con, clips, log=print) -> None:
    """Download (if we don't already have the footage) and edit each clip in its layout."""
    for clip in clips:
        log(f"[{clip.id}] {clip.title!r} ({clip.views} views)")
        src = out = None
        try:
            src = files.local(cfg, clip.src_path) if clip.src_path else None
            if not src or not src.exists():
                src = dl.download(clip, cfg.data_dir / "raw")
                db.update(con, clip.id, src_path=files.save(cfg, src, "raw"))
            layout, bg = clip.layout or cfg.render.get("layout", "framed"), None
            if layout == "split":
                available = files.backgrounds(cfg)
                name = clip.background or cfg.render.get("background", "")
                if name not in available:
                    name = next(iter(available), "")
                bg = files.local(cfg, available[name]) if name else None
            out = rn.render(src, cfg.data_dir / "out" / f"{src.stem}.mp4", clip.title, clip.credit,
                            cfg.max_duration, cfg.render.get("font", ""), layout, bg)
            ref = files.save(cfg, out, "out")
            if clip.out_path and clip.out_path != ref:
                files.remove(cfg, clip.out_path)   # the previous edit of this clip
            status = "approved" if cfg.auto_approve else "rendered"
            db.update(con, clip.id, status=status, out_path=ref, error="")
            log(f"  ready [{layout}, {status}]")
        except Exception as e:
            db.update(con, clip.id, status="failed", error=str(e)[:500])
            log(f"  FAILED: {e}")
        finally:
            files.tidy(out, src)  # cloud only: the copies in storage are what we keep


def publish_clip(cfg: Config, con, clip: db.Clip, targets: list[str], log=print) -> None:
    """Post one clip to every target account it hasn't already been posted to."""
    done = db.posts(con, clip.id)
    todo = [name for name in targets if not done.get(name, {}).get("ref")]
    if todo:
        video = None
        try:
            video = files.local(cfg, clip.out_path)
            local_clip = dataclasses.replace(clip, out_path=str(video))
            for name in todo:
                try:
                    ref = publish.PLATFORMS[name].upload(cfg, local_clip)
                    db.record_post(con, clip.id, name, ref=ref)
                    log(f"[{clip.id}] posted to {name}: {ref}")
                except Exception as e:
                    db.record_post(con, clip.id, name, error=str(e)[:500])
                    log(f"[{clip.id}] {name} FAILED: {e}")
        finally:
            if video is not None and files.is_url(clip.out_path):
                files.tidy(video)
    done = db.posts(con, clip.id)
    if all(done.get(name, {}).get("ref") for name in targets):
        db.update(con, clip.id, status="published", published_url=done[targets[0]]["ref"])


def publish_approved(cfg: Config, con, limit: int = 3, log=print) -> None:
    """Post each approved clip to every connected account it hasn't already been posted to."""
    targets = publish.connected(cfg)
    if not targets:
        log("No social accounts connected yet.")
        return
    clips = db.by_status(con, "approved", limit)
    if not clips:
        log("Nothing approved to post.")
    for clip in clips:
        publish_clip(cfg, con, clip, targets, log)
