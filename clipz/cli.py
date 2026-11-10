from __future__ import annotations

import argparse
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from . import config, db
from .db import Clip


def cmd_run(cfg, con, args):
    """discover -> download -> render for the top-ranked new clips."""
    from . import sources

    if not cfg.creators:
        raise SystemExit("no [[creators]] in config.toml - add the channels you're allowed to clip")
    print("Discovering...")
    new = sum(db.add(con, c) for c in sources.discover(cfg))
    print(f"{new} new clip(s) queued")
    _process(cfg, con, db.by_status(con, "discovered", cfg.max_clips_per_run))


def _process(cfg, con, clips):
    from . import download as dl, render as rn

    for clip in clips:
        print(f"[{clip.id}] {clip.title!r} ({clip.views} views)")
        try:
            src = dl.download(clip, cfg.data_dir / "raw")
            db.update(con, clip.id, status="downloaded", src_path=str(src))
            out = rn.render(src, cfg.data_dir / "out" / f"{src.stem}.mp4", clip.title, clip.credit,
                            cfg.max_duration, cfg.render.get("font", ""))
            status = "approved" if cfg.auto_approve else "rendered"
            db.update(con, clip.id, status=status, out_path=str(out), error="")
            print(f"  -> {out} [{status}]")
        except Exception as e:
            db.update(con, clip.id, status="failed", error=str(e)[:500])
            print(f"  FAILED: {e}")


def cmd_add(cfg, con, args):
    """Queue a clip URL by hand (any site yt-dlp supports) and render it."""
    clip = Clip(
        id=f"manual:{hashlib.sha1(args.url.encode()).hexdigest()[:10]}", platform="manual",
        creator=args.credit, credit=args.credit, title=args.title, url=args.url,
        created_at=datetime.now(timezone.utc).isoformat(), start=args.start, end=args.end,
    )
    if not db.add(con, clip):
        raise SystemExit(f"already queued as {clip.id}")
    _process(cfg, con, [clip])


def cmd_list(cfg, con, args):
    clips = db.by_status(con, args.status) if args.status else db.all_clips(con)
    for c in clips:
        extra = c.published_url or c.out_path or c.error
        print(f"{c.status:<10} {c.id:<40} {c.views:>7}v  {c.title[:40]:<40}  {extra}")
    if not clips:
        print("nothing here")


def _set_status(con, clip_id, allowed_from, to):
    clip = db.get(con, clip_id)
    if not clip:
        raise SystemExit(f"no clip with id {clip_id}")
    if clip.status not in allowed_from:
        raise SystemExit(f"{clip_id} is {clip.status!r}; expected one of {allowed_from}")
    db.update(con, clip_id, status=to)
    print(f"{clip_id}: {clip.status} -> {to}")


def cmd_approve(cfg, con, args):
    ids = [c.id for c in db.by_status(con, "rendered")] if args.ids == ["all"] else args.ids
    for i in ids:
        _set_status(con, i, ("rendered", "rejected"), "approved")


def cmd_reject(cfg, con, args):
    for i in args.ids:
        _set_status(con, i, ("rendered", "approved"), "rejected")


def cmd_retry(cfg, con, args):
    """Re-run download + render for clips that failed (e.g. a network blip)."""
    _process(cfg, con, db.by_status(con, "failed"))


def cmd_publish(cfg, con, args):
    from . import publish

    clips = db.by_status(con, "approved", args.limit)
    if not clips:
        print("nothing approved - see `clipz list --status rendered` and `clipz approve <id>`")
    for clip in clips:
        try:
            url = publish.upload(cfg, clip)
            db.update(con, clip.id, status="published", published_url=url)
            print(f"[{clip.id}] published: {url}")
        except Exception as e:
            db.update(con, clip.id, error=str(e)[:500])
            print(f"[{clip.id}] upload FAILED: {e}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="clipz", description=__doc__)
    p.add_argument("-c", "--config", default="config.toml")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("run", help="discover, download and render top new clips").set_defaults(fn=cmd_run)

    a = sub.add_parser("add", help="queue and render one clip URL by hand")
    a.add_argument("url")
    a.add_argument("--title", required=True, help="headline shown on the video")
    a.add_argument("--credit", required=True, help="creator credit, e.g. kick.com/someone")
    a.add_argument("--start", type=float, help="cut start in seconds (for long videos)")
    a.add_argument("--end", type=float, help="cut end in seconds")
    a.set_defaults(fn=cmd_add)

    l = sub.add_parser("list", help="show the queue")
    l.add_argument("--status")
    l.set_defaults(fn=cmd_list)

    ap = sub.add_parser("approve", help="approve rendered clips for publishing ('all' = every rendered clip)")
    ap.add_argument("ids", nargs="+")
    ap.set_defaults(fn=cmd_approve)

    rj = sub.add_parser("reject", help="reject clips so they are never published")
    rj.add_argument("ids", nargs="+")
    rj.set_defaults(fn=cmd_reject)

    sub.add_parser("retry", help="re-process failed clips").set_defaults(fn=cmd_retry)

    pb = sub.add_parser("publish", help="upload approved clips to YouTube Shorts")
    pb.add_argument("--limit", type=int, default=3, help="max uploads this run (default 3)")
    pb.set_defaults(fn=cmd_publish)

    args = p.parse_args(argv)
    if (args.cmd == "add") and ((args.start is None) != (args.end is None)):
        p.error("--start and --end must be given together")
    cfg = config.load(args.config)
    con = db.connect(cfg.db_path)
    args.fn(cfg, con, args)


if __name__ == "__main__":
    main()
