"""The cloud (Vercel) side: Postgres, the step-by-step worker, Blob storage and the app's
cloud mode. Postgres tests run when CLIPZ_TEST_DATABASE_URL points at a scratch database."""
import json
import os
import time

import pytest

from clipz import config, db, files, pipeline, worker
from clipz.app import create_app, create_vercel_app
from clipz.db import Clip

PG = os.environ.get("CLIPZ_TEST_DATABASE_URL")
BACKENDS = ["sqlite", pytest.param("postgres", marks=pytest.mark.skipif(not PG, reason="no test Postgres"))]


def fresh_pg():
    import psycopg
    with psycopg.connect(PG, autocommit=True) as c:
        for t in ("clips", "posts", "kv", "locks", "log"):
            c.execute(f"DROP TABLE IF EXISTS {t}")


@pytest.fixture(params=BACKENDS)
def con(request, tmp_path):
    if request.param == "postgres":
        fresh_pg()
        return db.connect(PG)
    return db.connect(tmp_path / "t.sqlite")


def clip(i="a", **kw):
    return Clip(id=f"twitch:{i}", platform="twitch", creator="x", credit="twitch.tv/x", title="t", url="u", **kw)


def test_database_round_trip_on_both_engines(con):
    assert db.add(con, clip(views=60, score=1.0, start=1.5, end=9.0)) is True
    assert db.add(con, clip(views=900, score=50.0)) is False          # refreshes the waiting clip
    c = db.get(con, "twitch:a")
    assert (c.views, c.score, c.start, c.end, c.src_path) == (900, 50.0, 1.5, 9.0, "")
    db.update(con, "twitch:a", status="queued", layout="split")
    assert [x.id for x in db.by_status(con, "queued")] == ["twitch:a"]
    with pytest.raises(KeyError):
        db.update(con, "nope", status="x")
    db.record_post(con, "twitch:a", "youtube", error="boom")
    db.record_post(con, "twitch:a", "youtube", ref="https://y/1")
    assert db.posts(con, "twitch:a") == {"youtube": {"ref": "https://y/1", "error": ""}}
    db.kv_set(con, "k", {"a": [1, 2]}); db.kv_set(con, "k", {"a": [3]})
    assert db.kv_get(con, "k") == {"a": [3]} and db.kv_get(con, "missing", 7) == 7
    db.kv_delete(con, "k")
    assert db.kv_get(con, "k") is None


def test_lock_is_exclusive_until_released_or_expired(con):
    assert db.lock_acquire(con, "w", "one", "Finding", ttl=60)
    assert not db.lock_acquire(con, "w", "two", "x", ttl=60)
    assert db.lock_label(con, "w") == "Finding"
    db.lock_release(con, "w", "one")
    assert db.lock_label(con, "w") == ""
    assert db.lock_acquire(con, "w", "two", "x", ttl=-1)               # already expired...
    assert db.lock_acquire(con, "w", "three", "y", ttl=60)             # ...so it can be taken over


def test_log_keeps_newest_entries_in_order(con, monkeypatch):
    monkeypatch.setattr(db, "LOG_KEEP", 3)
    for i in range(5):
        db.log_append(con, f"m{i}")
    assert [m for _, m in db.log_tail(con)] == ["m2", "m3", "m4"]


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for var in ("BLOB_READ_WRITE_TOKEN", "VERCEL_BLOB_READ_WRITE_TOKEN", "BLOB_STORE_ID"):
        monkeypatch.delenv(var, raising=False)
    return config.load("config.toml")


def test_worker_finds_queues_edits_and_posts_once_per_request(cfg, monkeypatch):
    con = cfg.connect()
    config.save_settings(cfg, {"creators": [{"platform": "twitch", "channel": "x"}], "max_clips_per_run": 2})
    cfg = config.load("config.toml")
    found = [clip(i, score=s) for i, s in (("a", 3), ("b", 2), ("c", 1))]
    monkeypatch.setattr(pipeline.sources, "discover", lambda c, log: found)
    edited, posted = [], []

    def fake_process(c, con, clips, log):
        for x in clips:
            edited.append(x.id)
            db.update(con, x.id, status="approved", out_path="/v.mp4")
    monkeypatch.setattr(pipeline, "process", fake_process)
    monkeypatch.setattr(worker.publish, "connected", lambda c: ["youtube"])
    monkeypatch.setattr(pipeline, "publish_clip", lambda c, con, x, t, log: posted.append(x.id))

    worker.request_find(cfg)
    assert worker.pending(cfg)
    log = []
    assert worker.run(cfg, log.append) == "done"
    assert edited == ["twitch:a", "twitch:b"]                          # top 2 only
    assert db.get(con, "twitch:c").status == "discovered"
    worker.request_publish(cfg)
    assert worker.run(cfg, log.append) == "done"
    assert posted == ["twitch:a", "twitch:b"]                          # publish_clip fake never succeeds...
    assert not worker.pending(cfg)                                     # ...but it still stops


def test_worker_stops_when_its_time_is_up_and_respects_the_lock(cfg, monkeypatch):
    con = cfg.connect()
    for i in "abc":
        db.add(con, clip(i, status="queued"))
    monkeypatch.setattr(pipeline, "process",
                        lambda c, con, clips, log: [db.update(con, x.id, status="rendered") for x in clips])
    assert worker.run(cfg, print, budget=-1) == "more"                 # one step, then hand off
    assert len(db.by_status(con, "queued")) == 2
    db.lock_acquire(con, worker.LOCK, "someone-else", "Editing", ttl=60)
    assert worker.run(cfg, print) == "busy"


def test_files_go_to_blob_in_the_cloud(cfg, monkeypatch, tmp_path):
    video = tmp_path / "v.mp4"; video.write_bytes(b"x")
    assert files.save(cfg, video, "out") == str(video)                 # local: just the path
    calls = {}

    class FakeBlob:
        @staticmethod
        def put_blob(path, data, **kw):
            calls["put"] = (path, data, kw["access"], kw["add_random_suffix"])
            return type("R", (), {"url": "https://s.public.blob.vercel-storage.com/out/v-RANDOM.mp4"})
        @staticmethod
        def delete_blob(url):
            calls["delete"] = url

    import vercel.blob
    monkeypatch.setattr(vercel.blob, "sync", FakeBlob, raising=False)
    monkeypatch.setitem(__import__("sys").modules, "vercel.blob.sync", FakeBlob)
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "t")
    ref = files.save(cfg, video, "out")
    assert ref.startswith("https://") and calls["put"] == ("out/v.mp4", b"x", "public", True)
    files.remove(cfg, ref)
    assert calls["delete"] == ref
    files.tidy(video)
    assert not video.exists()


# ---- the app in cloud mode ---------------------------------------------------------------------

@pytest.fixture
def cloud_app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CRON_SECRET", "cron-s3cret")
    woke = []
    app = create_app(str(tmp_path / "config.toml"), password="pw", cloud=True, wake=lambda: woke.append(1))
    client = app.test_client()
    base = {"base_url": "https://clipz.example"}
    client.post("/login", data={"password": "pw"}, **base)
    return client, woke, base


def test_buttons_record_the_work_and_wake_a_worker(cloud_app):
    client, woke, base = cloud_app
    r = client.post("/api/run", data="{}", content_type="application/json", headers={"X-Clipz": "1"}, **base)
    assert r.get_json() == {"started": True} and woke == [1]
    assert worker.pending(config.load("config.toml"))


def test_worker_and_cron_endpoints_need_the_secret(cloud_app, monkeypatch):
    client, woke, base = cloud_app
    anon = client.application.test_client()
    assert anon.post("/api/worker", **base).status_code == 403
    assert anon.get("/api/cron", headers={"Authorization": "Bearer wrong"}, **base).status_code == 403
    monkeypatch.setattr(worker, "run", lambda c, log, budget=None: "more")
    r = anon.get("/api/cron", headers={"Authorization": "Bearer cron-s3cret"}, **base)
    assert r.get_json() == {"result": "more"} and woke == [1]           # out of time: hands off


def test_state_restarts_stalled_work_but_not_too_often(cloud_app):
    client, woke, base = cloud_app
    worker.request_find(config.load("config.toml"))
    state = client.get("/api/state", **base).get_json()
    assert state["cloud"] is True and state["job"] == "Starting" and woke == [1]
    client.get("/api/state", **base)
    assert woke == [1]                                                  # cooldown


def test_cloud_videos_are_served_from_storage(cloud_app):
    client, _, base = cloud_app
    con = config.load("config.toml").connect()
    db.add(con, clip(status="rendered", out_path="https://s.public.blob.vercel-storage.com/out/a-X.mp4"))
    c = client.get("/api/state", **base).get_json()["clips"][0]
    assert c["media_url"] == "https://s.public.blob.vercel-storage.com/out/a-X.mp4"
    assert client.get("/media/twitch:a", **base).headers["Location"] == c["media_url"]


def test_gameplay_from_link(cloud_app, monkeypatch, tmp_path):
    client, _, base = cloud_app
    from clipz import download

    def fake_fetch(url, dest, name, max_seconds=180):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / name).write_bytes(b"v")
        return dest / name
    monkeypatch.setattr(download, "fetch_video", fake_fetch)
    post = lambda body: client.post("/api/backgrounds/link", data=json.dumps(body), content_type="application/json",
                                    headers={"X-Clipz": "1"}, **base)
    assert post({"url": "file:///etc/passwd"}).status_code == 400
    r = post({"url": "https://example.com/videos/My Run.mp4?x=1"})
    assert r.status_code == 200 and r.get_json()["name"].startswith("My_Run-")
    assert client.get("/api/state", **base).get_json()["backgrounds"] == [r.get_json()["name"]]


def test_oauth_state_survives_in_the_signed_cookie(cloud_app, monkeypatch):
    client, _, base = cloud_app
    from clipz.publish import youtube
    config.save_settings(config.load("config.toml"), {"youtube_upload": {"client_id": "cid", "client_secret": "s"}})
    got = {}
    monkeypatch.setattr(youtube, "finish_connect", lambda c, code, verifier, redirect: got.update(code=code, redirect=redirect))
    r = client.get("/connect/youtube", **base)
    from urllib.parse import parse_qs, urlparse
    state = parse_qs(urlparse(r.headers["Location"]).query)["state"][0]
    client.get("/oauth/youtube/callback?state=wrong&code=c", **base)
    assert got == {}                                                    # a forged return is ignored
    client.get("/connect/youtube", **base)
    r = client.get("/connect/youtube", **base)
    state = parse_qs(urlparse(r.headers["Location"]).query)["state"][0]
    client.get(f"/oauth/youtube/callback?state={state}&code=c", **base)
    assert got == {"code": "c", "redirect": "https://clipz.example/oauth/youtube/callback"}


def test_cloud_mode_never_runs_without_a_password(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = create_app(str(tmp_path / "config.toml"), password="", cloud=True, wake=lambda: None)
    assert app.test_client().get("/api/state", base_url="https://x.example").status_code == 403


def test_vercel_shows_setup_checklist_until_configured(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for var in ("CLIPZ_PASSWORD", "DATABASE_URL", "POSTGRES_URL", "BLOB_READ_WRITE_TOKEN", "BLOB_STORE_ID"):
        monkeypatch.delenv(var, raising=False)
    page = create_vercel_app().test_client().get("/anything").get_data(as_text=True)
    assert "CLIPZ_PASSWORD" in page and "Neon" in page and "Blob" in page


# ---- YouTube upload without Google's client library ---------------------------------------------

def test_youtube_refreshes_token_and_uploads_resumably(cfg, monkeypatch, tmp_path):
    from clipz.publish import tokens, youtube
    cfg.youtube_upload.update(client_id="cid", client_secret="sec")
    tokens.save(cfg, "youtube", {"access_token": "old", "refresh_token": "r", "expires_at": time.time() - 5})
    video = tmp_path / "v.mp4"; video.write_bytes(b"12345")
    seen = []

    class Reply:
        def __init__(self, code, data=None, headers=None):
            self.status_code, self._data, self.headers = code, data or {}, headers or {}
        def json(self):
            return self._data

    def post(url, data=None, json=None, headers=None, params=None, **kw):
        if url == youtube.TOKEN_URL:
            assert data["grant_type"] == "refresh_token" and data["refresh_token"] == "r"
            return Reply(200, {"access_token": "new", "expires_in": 3600})
        seen.append(("start", headers["Authorization"], headers["X-Upload-Content-Length"], json["snippet"]["categoryId"]))
        return Reply(200, headers={"Location": "https://upload.example/session"})

    def put(url, data=None, headers=None, **kw):
        seen.append(("put", url, data.read()))
        return Reply(200, {"id": "VID"})

    monkeypatch.setattr(youtube.requests, "post", post)
    monkeypatch.setattr(youtube.requests, "put", put)
    c = clip(out_path=str(video))
    assert youtube.upload(cfg, c) == "https://youtube.com/shorts/VID"
    assert seen == [("start", "Bearer new", "5", "20"), ("put", "https://upload.example/session", b"12345")]
    assert tokens.load(cfg, "youtube")["access_token"] == "new"


@pytest.mark.parametrize("layout", ["framed", "split"])
def test_real_edit_through_the_pipeline(cfg, tmp_path, layout):
    """No stubs: footage already downloaded -> edited video, in each layout."""
    import subprocess
    src, bg = tmp_path / "src.mp4", tmp_path / "bg.mp4"
    for path in (src, bg):
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=s=640x360:d=2",
                        "-f", "lavfi", "-i", "sine=d=2", "-shortest", "-pix_fmt", "yuv420p", str(path)], check=True)
    files.add_background(cfg, bg, "bg.mp4")
    con = cfg.connect()
    db.add(con, clip(status="queued", src_path=str(src), layout=layout))
    log = []
    pipeline.process(cfg, con, db.by_status(con, "queued"), log.append)
    done = db.get(con, "twitch:a")
    assert (done.status, done.error) == ("rendered", ""), log
    assert os.path.getsize(done.out_path) > 1000


def test_blob_key_found_under_a_custom_prefix(monkeypatch):
    for var in ("BLOB_READ_WRITE_TOKEN", "VERCEL_BLOB_READ_WRITE_TOKEN", "BLOB_STORE_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CLIPZ_VIDEOS_READ_WRITE_TOKEN", "vercel_blob_rw_abc_123")
    monkeypatch.setenv("OTHER_READ_WRITE_TOKEN", "not-a-blob-key")
    assert files.cloud()
    assert os.environ["BLOB_READ_WRITE_TOKEN"] == "vercel_blob_rw_abc_123"


def test_blob_store_id_connection_counts_as_connected(monkeypatch):
    for var in list(os.environ):
        if var.endswith(("_READ_WRITE_TOKEN", "_STORE_ID")):
            monkeypatch.delenv(var)
    assert not files.cloud()
    monkeypatch.setenv("CLIPZ_VIDEOS_STORE_ID", "store_AbC123")
    assert files.cloud() and os.environ["BLOB_STORE_ID"] == "store_AbC123"


# ---- Browse, categories and searching every platform -------------------------------------------

def test_browse_lists_finds_by_category_and_edit_skip_work(cloud_app):
    client, woke, base = cloud_app
    c = config.load("config.toml")
    config.save_settings(c, {"creators": [
        {"platform": "twitch", "channel": "togi", "category": "IRL"},
        {"platform": "twitch", "channel": "gtaguy", "category": "GTA 6"}]})
    con = c.connect()
    db.add(con, Clip(id="twitch:1", platform="twitch", creator="togi", credit="x", title="a", url="u", score=2))
    db.add(con, Clip(id="twitch:2", platform="twitch", creator="gtaguy", credit="x", title="b", url="u", score=1))
    db.add(con, Clip(id="twitch:3", platform="twitch", creator="gtaguy", credit="x", title="c", url="u", status="skipped"))
    get = lambda q="": client.get("/api/browse" + q, **base).get_json()
    assert [x["id"] for x in get()["clips"]] == ["twitch:1", "twitch:2"]          # skipped ones hidden
    assert [x["id"] for x in get("?category=IRL")["clips"]] == ["twitch:1"]
    assert client.get("/api/state", **base).get_json()["categories"] == ["GTA 6", "IRL"]
    act = lambda cid, a: client.post(f"/api/clips/{cid}/{a}", data="{}", content_type="application/json",
                                     headers={"X-Clipz": "1"}, **base)
    assert act("twitch:1", "edit").status_code == 200 and woke                       # starts editing
    assert db.get(con, "twitch:1").status == "queued"
    assert act("twitch:2", "skip").status_code == 200
    assert get()["total"] == 1


def test_youtube_channel_search_and_clips_from_other_uploaders(cfg, monkeypatch):
    from clipz.config import Creator
    from clipz.sources import youtube
    calls = []

    def fake_search(query, sp, limit):
        calls.append((query, sp))
        if sp == youtube.CHANNELS:
            return [{"uploader_id": "@TOGI", "channel": "TOGI", "channel_follower_count": 1200000,
                     "thumbnails": [{"url": "//img/1.jpg"}]},
                    {"uploader_id": None, "channel_url": "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv", "channel": "Togi Clips"}]
        now = time.time()
        return [
            {"id": "v1", "title": "TOGI loses it", "duration": 40, "view_count": 900, "timestamp": now - 3600, "channel": "ClipKing", "uploader_id": "@clipking"},
            {"id": "v2", "title": "TOGI full stream", "duration": 7200, "view_count": 9000, "timestamp": now - 3600, "channel": "TOGI"},
            {"id": "v3", "title": "unrelated video", "duration": 30, "view_count": 900, "timestamp": now - 3600, "channel": "X"},
            {"id": "v4", "title": "Togi on his own channel", "duration": 50, "view_count": 900, "timestamp": now - 3600, "channel": "TOGI", "uploader_id": "@TOGI"}]
    monkeypatch.setattr(youtube, "_search", fake_search)
    found = youtube.search_channels(cfg, "togi")
    assert found[0] == {"login": "@TOGI", "name": "TOGI", "image": "https://img/1.jpg", "live": False,
                        "game": "1,200,000 subscribers"}
    assert found[1]["login"].endswith("UCabcdefghijklmnopqrstuv")
    assert youtube.search_channels(cfg, "to") == []                                   # too short to bother

    cfg.min_views, cfg.game = 50, ""
    creator = Creator("youtube", "@TOGI", name="TOGI", all_content=True)
    clips = youtube._others_uploads(cfg, creator, datetime_now())
    assert [c.id for c in clips] == ["youtube:v1", "youtube:v4"]                      # too long / off-topic dropped
    assert clips[0].uploader == "ClipKing" and clips[1].uploader == ""                 # other clipper credited
    assert clips[0].preview.startswith("https://www.youtube.com/embed/v1")
    assert db.credit_line(clips[0]) == "youtube.com/@TOGI (clip via ClipKing)"


def test_kick_channel_search_parses_and_sorts_live_first(cfg, monkeypatch):
    from clipz.sources import kick
    monkeypatch.setattr(kick, "_get", lambda url, **p: {"channels": [
        {"slug": "togi_fan", "is_live": False, "followers_count": 10, "user": {"username": "Togi_Fan"}},
        {"slug": "togi", "is_live": True, "followers_count": 250000, "user": {"username": "TOGI", "profile_pic": "p.jpg"}}]})
    res = kick.search_channels(cfg, "togi")
    assert [r["login"] for r in res] == ["togi", "togi_fan"]
    assert res[0] == {"login": "togi", "name": "TOGI", "image": "p.jpg", "live": True, "game": "250,000 followers"}


def test_old_finds_are_pruned(con):
    db.add(con, clip("old", created_at="2020-01-01T00:00:00+00:00"))
    db.add(con, clip("kept", created_at="2020-01-01T00:00:00+00:00", status="rendered"))
    db.add(con, clip("new", created_at="2099-01-01T00:00:00+00:00"))
    assert db.prune(con, "2024-01-01T00:00:00+00:00") == 1
    assert sorted(c.id for c in db.all_clips(con)) == ["twitch:kept", "twitch:new"]


def datetime_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


# ---- Following games ----------------------------------------------------------------------------

def test_follow_a_game_and_get_top_clips_from_everyone(cfg, monkeypatch):
    from clipz import sources
    from clipz.sources import twitch, youtube
    config.save_settings(cfg, {"twitch": {"client_id": "cid", "client_secret": "s"},
                               "creators": [{"platform": "twitch", "channel": "togi", "category": "IRL"}],
                               "games": [{"name": "Grand Theft Auto VI", "twitch_id": "999", "label": "GTA 6"}]})
    cfg = config.load("config.toml")
    now = datetime_now().strftime("%Y-%m-%dT%H:%M:%SZ")
    clip_row = lambda cid, who, views: {"id": cid, "title": f"{who} clip", "url": "u", "view_count": views,
                                        "created_at": now, "duration": 25, "broadcaster_name": who, "game_id": "999"}
    asked = []

    def fake_get(s, path, **p):
        asked.append((path, p.get("game_id"), p.get("broadcaster_id")))
        if path == "users":
            return [{"login": "togi", "id": "1"}]
        if path == "games":
            return [{"id": "999"}]
        if p.get("game_id"):
            return [clip_row("shared", "Togi", 5000), clip_row("g1", "BigStreamer", 90000), clip_row("tiny", "Nobody", 3)]
        return [clip_row("shared", "Togi", 5000)]
    monkeypatch.setattr(twitch, "_session", lambda c: None)
    monkeypatch.setattr(twitch, "_get", fake_get)
    monkeypatch.setattr(youtube, "_search", lambda q, sp, limit: [])
    found = sources.discover(cfg, log=lambda m: None)
    by_id = {c.id: c for c in found}
    assert set(by_id) == {"twitch:shared", "twitch:g1"}                    # under min views dropped
    assert by_id["twitch:g1"].category == "GTA 6" and by_id["twitch:g1"].credit == "twitch.tv/bigstreamer"
    con = cfg.connect()
    for c in found:
        db.add(con, c)
    assert cfg.category_of(db.get(con, "twitch:shared")) == "IRL"         # streamer's category wins


def test_youtube_filter_is_most_viewed_within_the_window():
    from clipz.sources import youtube
    assert youtube.videos_filter(24) == "CAMSBAgCEAE%3D"      # most viewed, today, videos
    assert youtube.videos_filter(168) == "CAMSBAgDEAE%3D"     # ...this week
    assert youtube.videos_filter(720) == "CAMSBAgEEAE%3D"     # ...this month


def test_game_follow_endpoints_and_streamer_filter_toggle(cloud_app, monkeypatch):
    client, _, base = cloud_app
    send = lambda method, url, body: getattr(client, method)(url, data=json.dumps(body), content_type="application/json",
                                                            headers={"X-Clipz": "1"}, **base)
    assert send("post", "/api/games", {"name": ""}).status_code == 400
    assert send("post", "/api/games", {"id": "999", "name": "Grand Theft Auto VI", "label": "GTA 6"}).status_code == 200
    assert send("post", "/api/games", {"id": "999", "name": "grand theft auto vi"}).status_code == 400   # already following
    state = client.get("/api/state", **base).get_json()
    assert state["games"][0]["label"] == "GTA 6" and "GTA 6" in state["categories"]
    send("delete", "/api/games", {"name": "Grand Theft Auto VI"})
    assert client.get("/api/state", **base).get_json()["games"] == []
    send("post", "/api/creators", {"platform": "twitch", "channel": "speed", "all_content": True})
    send("post", "/api/creators/toggle", {"platform": "twitch", "channel": "speed"})
    assert client.get("/api/state", **base).get_json()["creators"][0]["all_content"] is False
