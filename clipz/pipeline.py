"""The steps shared by the command line and the app."""
from __future__ import annotations

from pathlib import Path

from . import db, download as dl, publish, render as rn, sources
from .config import Config


def find_and_render(cfg: Config, con, log=print) -> None:
    if not cfg.creators:
        log("No streamers on your list yet - add some first.")
        return
    log("Looking for new clips...")
    new = sum(db.add(con, c) for c in sources.discover(cfg, log))
    log(f"{new} new clip(s) found")
    process(cfg, con, db.by_status(con, "discovered", cfg.max_clips_per_run), log)


def backgrounds(cfg: Config) -> list[str]:
    """Gameplay videos available for the split layout."""
    folder = cfg.data_dir / "backgrounds"
    folder.mkdir(parents=True, exist_ok=True)
    return sorted(p.name for p in folder.iterdir() if p.suffix.lower() in rn.BACKGROUND_TYPES)


def process(cfg: Config, con, clips, log=print) -> None:
    """Download (if we don't already have the footage) and render each clip in its layout."""
    for clip in clips:
        log(f"[{clip.id}] {clip.title!r} ({clip.views} views)")
        try:
            src = Path(clip.src_path) if clip.src_path else None
            if not src or not src.exists():
                src = dl.download(clip, cfg.data_dir / "raw")
                db.update(con, clip.id, status="downloaded", src_path=str(src))
            layout = clip.layout or cfg.render.get("layout", "framed")
            background = None
            if layout == "split":
                name = clip.background or cfg.render.get("background", "")
                available = backgrounds(cfg)
                if name not in available:
                    name = available[0] if available else ""
                background = cfg.data_dir / "backgrounds" / name if name else None
            out = rn.render(src, cfg.data_dir / "out" / f"{src.stem}.mp4", clip.title, clip.credit,
                            cfg.max_duration, cfg.render.get("font", ""), layout, background)
            status = "approved" if cfg.auto_approve else "rendered"
            db.update(con, clip.id, status=status, out_path=str(out), error="")
            log(f"  ready: {out} [{layout}, {status}]")
        except Exception as e:
            db.update(con, clip.id, status="failed", error=str(e)[:500])
            log(f"  FAILED: {e}")


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
        done = db.posts(con, clip.id)
        for name in targets:
            if done.get(name, {}).get("ref"):
                continue
            try:
                ref = publish.PLATFORMS[name].upload(cfg, clip)
                db.record_post(con, clip.id, name, ref=ref)
                log(f"[{clip.id}] posted to {name}: {ref}")
            except Exception as e:
                db.record_post(con, clip.id, name, error=str(e)[:500])
                log(f"[{clip.id}] {name} FAILED: {e}")
        done = db.posts(con, clip.id)
        if all(done.get(name, {}).get("ref") for name in targets):
            first = done[targets[0]]["ref"]
            db.update(con, clip.id, status="published", published_url=first)
