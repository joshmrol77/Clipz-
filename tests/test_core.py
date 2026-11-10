import subprocess
from datetime import datetime, timezone

import pytest

from clipz import db, rank, render
from clipz.publish import youtube as publish
from clipz.db import Clip


def test_velocity_prefers_fast_recent_clips():
    now = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
    fresh = rank.velocity(1000, "2026-11-20T10:00:00Z", now)
    stale = rank.velocity(1000, "2026-11-19T12:00:00Z", now)
    assert fresh > stale


def test_best_window_skips_intro_and_finds_peak():
    heat = [{"start_time": i * 10.0, "end_time": i * 10.0 + 10, "value": 0.1} for i in range(30)]
    heat[0]["value"] = 1.0                      # intro spike must be ignored
    heat[12]["value"] = heat[13]["value"] = 0.9  # real peak at 120-140s
    start, end, _ = rank.best_window(heat, 30)
    assert start <= 120 and end >= 140


def test_db_dedupes_and_tracks_status(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    clip = Clip(id="twitch:abc", platform="twitch", creator="x", credit="x", title="t", url="u")
    assert db.add(con, clip) is True
    assert db.add(con, clip) is False
    db.update(con, "twitch:abc", status="rendered")
    assert [c.id for c in db.by_status(con, "rendered")] == ["twitch:abc"]
    with pytest.raises(KeyError):
        db.update(con, "nope", status="x")


def test_publish_metadata_credits_creator_and_fits_title_limit():
    clip = Clip(id="a", platform="twitch", creator="x", credit="twitch.tv/x", title="w" * 200, url="http://u")
    m = publish.metadata(clip, ["GTA6"])
    assert len(m["title"]) <= 100 and m["title"].endswith("#Shorts")
    assert "twitch.tv/x" in m["description"] and "http://u" in m["description"]


def test_render_produces_vertical_video(tmp_path):
    src, out = tmp_path / "src.mp4", tmp_path / "out.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=s=1280x720:d=3",
         "-f", "lavfi", "-i", "sine=d=3", "-shortest", "-pix_fmt", "yuv420p", str(src)], check=True)
    render.render(src, out, "He jumped: the 'whole' bridge, 100% clean", "twitch.tv/x", max_duration=2)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height:format=duration", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, check=True).stdout.split()
    assert probe[0] == "1080,1920"
    assert 1.5 < float(probe[1]) < 2.6


@pytest.mark.parametrize("layout", ["full", "split"])
def test_other_layouts_render_vertical_and_end_with_the_clip(tmp_path, layout):
    src, bg, out = tmp_path / "src.mp4", tmp_path / "bg.mp4", tmp_path / "out.mp4"
    for path, size, secs in ((src, "1280x720", 3), (bg, "640x360", 1)):   # bg shorter: must loop
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=s={size}:d={secs}",
             "-f", "lavfi", "-i", f"sine=d={secs}", "-shortest", "-pix_fmt", "yuv420p", str(path)], check=True)
    render.render(src, out, "A title", "twitch.tv/x", layout=layout, background=bg)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height:format=duration", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, check=True).stdout.split()
    assert probe[0] == "1080,1920"
    assert 2.5 < float(probe[1]) < 3.6


def test_split_layout_without_gameplay_video_explains_itself(tmp_path):
    with pytest.raises(RuntimeError, match="gameplay video"):
        render.render(tmp_path / "x.mp4", tmp_path / "o.mp4", "t", "c", layout="split")


def test_old_database_gains_new_columns(tmp_path):
    import sqlite3
    old = sqlite3.connect(tmp_path / "old.sqlite")
    cols = [c for c in db.COLUMNS if c not in ("layout", "background")]
    old.execute(f"CREATE TABLE clips ({', '.join(cols)})")
    old.execute(f"INSERT INTO clips (id, title, status) VALUES ('a', 't', 'rendered')")
    old.commit(); old.close()
    assert db.get(db.connect(tmp_path / "old.sqlite"), "a").layout == ""
