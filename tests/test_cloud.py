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
    fields = dict(id=f"twitch:{i}", platform="twitch", creator="x", credit="twitch.tv/x", title="t", url="u", language="en")
    return Clip(**{**fields, **kw})


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


def test_worker_finds_into_the_feed_and_posts_once_per_request(cfg, monkeypatch):
    con = cfg.connect()
    config.save_settings(cfg, {"creators": [{"platform": "twitch", "channel": "x"}], "max_clips_per_run": 2})
    cfg = config.load("config.toml")
    found = [clip(i, score=s) for i, s in (("a", 3), ("b", 2), ("c", 1))]
    monkeypatch.setattr(pipeline.sources, "discover", lambda c, log: found)
    edited, posted = [], []
    monkeypatch.setattr(pipeline, "process", lambda c, con, clips, log: edited.extend(x.id for x in clips))
    monkeypatch.setattr(worker.publish, "connected", lambda c: ["youtube"])
    monkeypatch.setattr(pipeline, "publish_clip", lambda c, con, x, t, log: posted.append(x.id))

    worker.request_find(cfg)
    assert worker.pending(cfg)
    assert worker.run(cfg, print) == "done"
    assert edited == []                                                # nothing is edited unless you pick it
    assert [c.id for c in db.by_status(con, "discovered")] == ["twitch:a", "twitch:b", "twitch:c"]
    for cid in ("twitch:a", "twitch:b"):
        db.update(con, cid, status="approved")
    worker.request_publish(cfg)
    assert worker.run(cfg, print) == "done"
    assert posted == ["twitch:a", "twitch:b"]                          # publish_clip fake never succeeds...
    assert not worker.pending(cfg)                                     # ...but it still stops


def test_clips_old_versions_auto_picked_go_back_to_the_feed(cfg, monkeypatch):
    monkeypatch.setattr(pipeline, "process", lambda c, con, clips, log: None)
    con = cfg.connect()
    db.add(con, clip("auto", status="queued"))
    db.add(con, clip("autofail", status="failed", error="Sign in to confirm you're not a bot"))
    db.add(con, clip("chosen", status="queued", approve="1", autopost="1"))
    db.add(con, clip("reedit", status="queued", layout="split", out_path="/old.mp4"))
    worker.step(cfg, con, lambda m: None, lambda label: None)
    statuses = {c.id: c.status for c in db.all_clips(con)}
    assert statuses["twitch:auto"] == "discovered"
    assert statuses["twitch:autofail"] == "discovered"
    assert statuses["twitch:chosen"] != "discovered" and statuses["twitch:reedit"] != "discovered"


def test_worker_stops_when_its_time_is_up_and_respects_the_lock(cfg, monkeypatch):
    con = cfg.connect()
    for i in "abc":
        db.add(con, clip(i, status="queued", approve="1"))
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

def test_feed_filters_by_game_and_creator_and_save_hide_seen_work(cloud_app):
    client, woke, base = cloud_app
    c = config.load("config.toml")
    config.save_settings(c, {"creators": [
        {"platform": "twitch", "channel": "togi", "name": "TOGI", "category": "IRL"},
        {"platform": "kick", "channel": "speed", "category": "IRL"}]})
    con = c.connect()
    add = lambda cid, plat, who, game, score, **kw: db.add(con, Clip(
        id=cid, platform=plat, creator=who, credit="x", title=cid, url="u", score=score, game=game, language="en", **kw))
    add("twitch:1", "twitch", "togi", "Just Chatting", 5)
    add("twitch:2", "twitch", "BigGuy", "GTA 6", 4, category="GTA 6")
    add("kick:3", "kick", "speed", "GTA 6", 3)
    add("youtube:4", "youtube", "togi", "", 2)
    add("twitch:5", "twitch", "togi", "Just Chatting", 1, status="skipped")
    get = lambda q="": client.get("/api/feed" + q, **base).get_json()
    send = lambda method, url, body=None: getattr(client, method)(url, data=json.dumps(body or {}), content_type="application/json",
                                                                 headers={"X-Clipz": "1"}, **base)
    assert [x["id"] for x in get()["clips"]] == ["twitch:1", "twitch:2", "kick:3", "youtube:4"]
    assert [x["id"] for x in get("?game=gta 6")["clips"]] == ["twitch:2", "kick:3"]
    assert [x["id"] for x in get("?creator=twitch:togi")["clips"]] == ["twitch:1"]
    assert [x["id"] for x in get("?creator=kick:speed&game=GTA 6")["clips"]] == ["kick:3"]
    filters = client.get("/api/feed/filters", **base).get_json()
    assert filters["games"][0] == {"name": "GTA 6", "count": 2}
    assert {"key": "kick:speed", "name": "speed", "platform": "kick", "count": 1} in filters["creators"]

    send("post", "/api/clips/seen", {"ids": ["twitch:1"]})                       # seen ones move to the back
    assert get()["clips"][-1]["id"] == "twitch:1"
    assert send("post", "/api/clips/twitch:2/save").status_code == 200 and woke  # save for later
    saved = db.get(con, "twitch:2")
    assert (saved.status, saved.approve) == ("queued", "")
    send("post", "/api/mute", {"kind": "creator", "value": "kick:speed", "label": "speed"})
    assert [x["id"] for x in get()["clips"]] == ["youtube:4", "twitch:1"]
    send("post", "/api/mute", {"kind": "game", "value": "Just Chatting"})
    assert [x["id"] for x in get()["clips"]] == ["youtube:4"]
    send("delete", "/api/mute", {"kind": "creator", "value": "kick:speed"})
    assert "kick:3" in [x["id"] for x in get()["clips"]]
    muted = client.get("/api/state", **base).get_json()["muted"]
    assert muted["games"] == ["Just Chatting"] and muted["creators"] == []


def test_saved_clips_are_ready_to_post_after_editing(cfg, monkeypatch, tmp_path):
    import subprocess
    src = tmp_path / "src.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=s=640x360:d=1",
                    "-f", "lavfi", "-i", "sine=d=1", "-shortest", "-pix_fmt", "yuv420p", str(src)], check=True)
    con = cfg.connect()
    db.add(con, clip(status="queued", src_path=str(src), approve="1"))
    pipeline.process(cfg, con, db.by_status(con, "queued"), lambda m: None)
    assert db.get(con, "twitch:a").status == "approved"


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
                                        "created_at": now, "duration": 25, "broadcaster_name": who, "game_id": "999", "language": "en"}
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
    monkeypatch.setattr(twitch, "_get_pages", lambda s, path, pages, **p: fake_get(s, path, **p))
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
    from clipz.sources import kick
    monkeypatch.setattr(kick, "search_categories", lambda q: [{"slug": "grand-theft-auto-vi", "name": "Grand Theft Auto VI"}])
    send = lambda method, url, body: getattr(client, method)(url, data=json.dumps(body), content_type="application/json",
                                                            headers={"X-Clipz": "1"}, **base)
    assert send("post", "/api/games", {"name": ""}).status_code == 400
    assert send("post", "/api/games", {"id": "999", "name": "Grand Theft Auto VI", "label": "GTA 6"}).status_code == 200
    assert send("post", "/api/games", {"id": "999", "name": "grand theft auto vi"}).status_code == 400   # already following
    state = client.get("/api/state", **base).get_json()
    assert state["games"][0]["label"] == "GTA 6" and "GTA 6" in state["categories"]
    assert state["games"][0]["kick_slug"] == "grand-theft-auto-vi"
    send("delete", "/api/games", {"name": "Grand Theft Auto VI"})
    assert client.get("/api/state", **base).get_json()["games"] == []
    send("post", "/api/creators", {"platform": "twitch", "channel": "speed", "all_content": True})
    send("post", "/api/creators/toggle", {"platform": "twitch", "channel": "speed"})
    assert client.get("/api/state", **base).get_json()["creators"][0]["all_content"] is False


def test_kick_clips_use_current_endpoints(cfg, monkeypatch):
    from clipz.config import Creator
    from clipz.sources import kick
    now = datetime_now().strftime("%Y-%m-%dT%H:%M:%SZ")
    calls = []

    def fake_get(url, **params):
        calls.append((url, params))
        if url.endswith("/api/v2/channels/speed"):
            return {"id": 42, "slug": "speed", "user": {"username": "Speed"}}
        if "/channels/42/clips" in url:
            return {"data": {"clips": [
                {"id": "clip_1", "title": "huge", "views_count": 9000, "created_at": now, "duration": 30,
                 "thumbnail_url": "t.jpg", "playback_url": "https://clips.kick.com/1.m3u8",
                 "category": {"name": "Grand Theft Auto VI"}, "channel": {"slug": "speed"}, "creator": {"username": "fan1"}}],
                "cursor": ""}}
        if "/categories/grand-theft-auto-vi/clips" in url:
            return {"data": {"clips": [
                {"id": "clip_2", "title": "heist", "views": 5000, "created_at": now, "duration": 20,
                 "channel": {"slug": "otherguy"}, "category": {"name": "Grand Theft Auto VI"}}]}}
        raise LookupError("not found on Kick")
    monkeypatch.setattr(kick, "_get", fake_get)
    cfg.games = [{"name": "Grand Theft Auto VI", "label": "GTA 6", "kick_slug": "grand-theft-auto-vi"}]
    cfg.min_views = 50
    [c1] = kick.discover(cfg, [Creator("kick", "speed", all_content=True)])
    assert calls[1][0] == "https://web.kick.com/api/v1/channels/42/clips" and calls[1][1]["sort"] == "views"
    assert (c1.id, c1.views, c1.uploader, c1.game, c1.url) == ("kick:clip_1", 9000, "fan1", "GTA 6", "https://kick.com/speed/clips/clip_1")
    assert c1.preview.endswith(".m3u8")
    [c2] = kick.discover_games(cfg, cfg.games)
    assert (c2.creator, c2.credit, c2.category, c2.game) == ("otherguy", "kick.com/otherguy", "GTA 6", "GTA 6")
    # exact channel names still come up when Kick's search finds nothing
    monkeypatch.setattr(kick, "_get", lambda url, **p: {"channels": []} if "search" in url else fake_get(url, **p))
    assert kick.search_channels(cfg, "speed")[0]["login"] == "speed"


# ---- Wednesday: open the app, see fresh clips, post in one go ----------------------------------

def test_post_from_feed_edits_in_chosen_format_then_posts(cloud_app):
    client, woke, base = cloud_app
    c = config.load("config.toml")
    con = c.connect()
    db.add(con, clip("new"))
    db.add(con, clip("edited", status="rendered", out_path="/x.mp4"))
    send = lambda url, body=None: client.post(url, data=json.dumps(body or {}), content_type="application/json",
                                              headers={"X-Clipz": "1"}, **base)
    assert send("/api/clips/twitch:new/post", {"layout": "nope"}).status_code == 400
    assert "gameplay video" in send("/api/clips/twitch:new/post", {"layout": "split"}).get_json()["error"]
    assert send("/api/clips/twitch:new/post", {"layout": "full", "title": "SPEED LOSES IT"}).status_code == 200
    new = db.get(con, "twitch:new")
    assert (new.status, new.approve, new.autopost, new.layout, new.title) == ("queued", "1", "1", "full", "SPEED LOSES IT")
    assert send("/api/clips/twitch:edited/post").status_code == 200                  # already edited: just post
    assert db.get(con, "twitch:edited").status == "approved"
    assert db.kv_get(con, worker.WANT_PUBLISH) is not None
    assert send("/api/clips/twitch:edited/keep").status_code == 400                   # only new finds
    db.add(con, clip("later"))
    assert send("/api/clips/twitch:later/keep", {"layout": "framed"}).status_code == 200
    later = db.get(con, "twitch:later")
    assert (later.status, later.approve, later.autopost) == ("queued", "", "")


def test_autopost_clip_is_posted_after_editing(cfg, monkeypatch):
    con = cfg.connect()
    db.add(con, clip(status="queued", approve="1", autopost="1"))
    monkeypatch.setattr(pipeline, "process", lambda c, con, clips, log: [db.update(con, x.id, status="approved") for x in clips])
    worker.step(cfg, con, print, lambda label: None)
    assert db.kv_get(con, worker.WANT_PUBLISH) == {"tried": []}


def test_opening_the_app_starts_a_search_when_it_has_been_a_while(cloud_app):
    client, woke, base = cloud_app
    c = config.load("config.toml")
    client.get("/api/state", **base)
    assert woke == []                                           # nothing followed yet: nothing to search
    config.save_settings(c, {"creators": [{"platform": "twitch", "channel": "ishowspeed"}]})
    state = client.get("/api/state", **base).get_json()
    assert state["job"] == "Finding clips" and woke == [1]
    assert db.kv_get(c.connect(), worker.WANT_FIND)
    db.kv_delete(c.connect(), worker.WANT_FIND)
    client.get("/api/state", **base)
    assert woke == [1]                                          # searched recently: no new search


def test_game_is_recognised_from_clip_titles(cfg):
    cfg.games = [{"name": "Grand Theft Auto VI", "label": "GTA 6"}]
    for title in ("IShowSpeed plays GTA 6 for the first time", "Speed in GTA VI is insane",
                  "SPEED GTA6 RAGE", "grand theft auto 6 funny moments", "Grand Theft Auto VI heist"):
        assert cfg.infer_game(title) == "GTA 6", title
    for title in ("Speed plays GTA 5", "GTA V stunts", "a gta 60 second clip", "Speed meets Ronaldo"):
        assert cfg.infer_game(title) == "", title


def test_feed_gets_direct_video_for_autoplay(cloud_app, monkeypatch):
    client, _, base = cloud_app
    from clipz import download
    calls = []
    monkeypatch.setattr(download, "stream_url", lambda url, max_height=720: calls.append(url) or "https://cdn/clip.mp4")
    con = config.load("config.toml").connect()
    db.add(con, Clip(id="twitch:a", platform="twitch", creator="x", credit="x", title="t", url="https://clips.twitch.tv/a"))
    db.add(con, Clip(id="kick:b", platform="kick", creator="x", credit="x", title="t", url="u", preview="https://k/b.m3u8"))
    db.add(con, Clip(id="youtube:c", platform="youtube", creator="x", credit="x", title="t", url="u"))
    get = lambda cid: client.get(f"/api/clips/{cid}/stream", **base).get_json()["url"]
    assert get("twitch:a") == "https://cdn/clip.mp4" and get("twitch:a") == "https://cdn/clip.mp4"
    assert calls == ["https://clips.twitch.tv/a"]                       # looked up once, then remembered
    assert get("kick:b") == "https://k/b.m3u8" and get("youtube:c") == ""


def test_english_only_keeps_english_clips(cfg):
    assert cfg.english_only
    yt = lambda title: clip(title[:8], platform="youtube", title=title, language="")
    keep = [clip("a", language="en"), clip("b", language="en-gb"),
            yt("Speed loses it 😂"), yt("SPEED GTA 6 RAGE"), yt("he really did that"), yt("Jynxzi crashout"),
            yt("No way he hit that shot"), yt("La La Land reaction gone wrong lol")]
    drop = [clip("e", language="es"), clip("old", language=""),          # Twitch with no language = from before
            yt("Спид проиграл в GTA 6"), yt("스피드 GTA 6 레전드"),
            yt("el mejor clip de la semana"), yt("ele não acredita kkkk"), yt("jajaja que risa"),
            yt("c'est pas possible"), yt("das ist nicht normal"), yt("increíble jugada")]
    assert [c.title for c in keep if not cfg.wanted_language(c)] == []
    assert [c.title or c.id for c in drop if cfg.wanted_language(c)] == []
    cfg.english_only = False
    assert all(cfg.wanted_language(c) for c in drop)


def test_feed_hides_non_english_clips_already_found(cloud_app):
    client, _, base = cloud_app
    con = config.load("config.toml").connect()
    db.add(con, clip("en", language="en"))
    db.add(con, clip("pt", language="pt"))
    assert [x["id"] for x in client.get("/api/feed", **base).get_json()["clips"]] == ["twitch:en"]


def test_youtube_shorts_without_length_or_views_are_kept(cfg, monkeypatch):
    from clipz.sources import youtube
    now = time.time()
    monkeypatch.setattr(youtube, "_search", lambda q, sp, limit: [
        {"id": "s1", "url": "https://www.youtube.com/shorts/s1", "title": "Fortnite insane clutch", "timestamp": now - 600},
        {"id": "v1", "url": "https://www.youtube.com/watch?v=v1", "title": "Fortnite no length", "timestamp": now - 600},
        {"id": "s2", "url": "https://www.youtube.com/shorts/s2", "title": "Fortnite few views", "view_count": 3}])
    cfg.min_views, cfg.games = 50, [{"name": "Fortnite", "label": "Fortnite"}]
    clips = youtube.discover_games(cfg, cfg.games)
    assert [c.id for c in clips] == ["youtube:s1", "youtube:v1"] and clips[0].duration == 60   # no length = a Short


def test_clip_actions_work_when_the_host_passes_the_path_still_encoded(cloud_app, monkeypatch):
    """Vercel gives the app "/api/clips/twitch%3Aa/..." without decoding it."""
    client, _, base = cloud_app
    from clipz import download
    monkeypatch.setattr(download, "stream_url", lambda url, max_height=720: "https://cdn/a.mp4")
    con = config.load("config.toml").connect()
    db.add(con, clip("a"))
    raw = lambda method, path: getattr(client, method)(path, data="{}", content_type="application/json", headers={"X-Clipz": "1"},
                                                      environ_overrides={"PATH_INFO": path}, **base)
    assert raw("get", "/api/clips/twitch%3Aa/stream").get_json()["url"] == "https://cdn/a.mp4"
    assert raw("post", "/api/clips/twitch%3Aa/skip").status_code == 200
    assert db.get(con, "twitch:a").status == "skipped"


def test_feed_view_ranges(cloud_app):
    client, _, base = cloud_app
    con = config.load("config.toml").connect()
    for i, views in enumerate((40, 100, 999, 5000, 250000)):
        db.add(con, clip(str(i), views=views, score=10 - i))
    get = lambda r: [x["views"] for x in client.get(f"/api/feed?views={r}", **base).get_json()["clips"]]
    assert get("0-100") == [40] and get("100-1000") == [100, 999] and get("100000-") == [250000]
    assert get("") == [40, 100, 999, 5000, 250000] and get("junk") == [40, 100, 999, 5000, 250000]
    counts = {v["range"]: v["count"] for v in client.get("/api/feed/filters", **base).get_json()["views"]}
    assert counts == {"0-100": 1, "100-1000": 2, "1000-10000": 1, "10000-100000": 0, "100000-": 1}


def test_heat_finds_breakout_clips_and_viral_filter(cloud_app):
    from datetime import datetime, timedelta, timezone
    from clipz import rank
    now = datetime.now(timezone.utc)
    ago = lambda h: (now - timedelta(hours=h)).isoformat()
    # a streamer whose clips usually get ~100 views/hour, and one clip doing 8,000 in 2 hours
    group = [clip(f"n{i}", views=1200, created_at=ago(12)) for i in range(5)] + [clip("hot", views=8000, created_at=ago(2))]
    rank.add_heat(group, lambda c: c.creator, now)
    hot = group[-1]
    assert hot.heat > 30 and all(c.heat == 1.0 for c in group[:-1])
    assert rank.is_viral(hot, now) and not rank.is_viral(group[0], now)
    assert rank.is_viral(clip("boom", views=60000, created_at=ago(3)), now)          # exploding on its own

    client, _, base = cloud_app
    con = config.load("config.toml").connect()
    for c in group:
        db.add(con, c)
    feed = client.get("/api/feed", **base).get_json()["clips"]
    assert feed[0]["id"] == "twitch:hot" and feed[0]["viral"] is True               # hottest first
    assert [x["id"] for x in client.get("/api/feed?viral=1", **base).get_json()["clips"]] == ["twitch:hot"]
    assert client.get("/api/feed/filters", **base).get_json()["viral"] == 1
