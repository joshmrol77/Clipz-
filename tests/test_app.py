import json
import shutil
import subprocess
from pathlib import Path

import pytest

from clipz import config, db, pipeline, publish
from clipz.app import create_app
from clipz.db import Clip
from clipz.publish import tiktok

JSON = {"content_type": "application/json"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = create_app(str(tmp_path / "config.toml"), port=8777)
    return Client(app.test_client()), tmp_path


class Client:
    """Test client that sends the Host header a real browser on this machine would."""
    def __init__(self, inner):
        self.inner = inner
    def __getattr__(self, method):
        def call(url, **kw):
            kw.setdefault("base_url", "http://127.0.0.1:8777")
            return getattr(self.inner, method)(url, **kw)
        return call


def post(client, url, body=None, method="post"):
    return getattr(client, method)(url, data=json.dumps(body or {}), **JSON)


def test_add_and_remove_streamer_requires_permission(env):
    client, _ = env
    assert post(client, "/api/creators", {"platform": "twitch", "channel": "abc"}).status_code == 400
    assert post(client, "/api/creators", {"platform": "twitch", "channel": "abc", "permission": True}).status_code == 200
    assert post(client, "/api/creators", {"platform": "twitch", "channel": "ABC", "permission": True}).status_code == 400
    creators = client.get("/api/state").get_json()["creators"]
    assert creators == [{"platform": "twitch", "channel": "abc", "credit": "twitch.tv/abc",
                         "all_content": False, "hashtags": []}]
    post(client, "/api/creators", creators[0], method="delete")
    assert client.get("/api/state").get_json()["creators"] == []


def test_settings_keep_secrets_private_and_blank_does_not_erase(env):
    client, _ = env
    post(client, "/api/settings", {"min_views": "500", "twitch": {"client_id": "id", "client_secret": "shh"}})
    post(client, "/api/settings", {"twitch": {"client_id": "id2", "client_secret": ""}})
    body = client.get("/api/state").get_data(as_text=True)
    assert "shh" not in body
    state = json.loads(body)["settings"]
    assert state["min_views"] == 500 and state["twitch"] == {"client_id": "id2", "client_secret_set": True}
    assert config.load("config.toml").twitch["client_secret"] == "shh"


def test_rejects_other_hosts_and_non_json_posts(env):
    client, _ = env
    assert client.get("/api/state", base_url="http://evil.example").status_code == 403
    assert client.post("/api/run", data="x=1").status_code == 415


def test_approve_reject_flow(env):
    client, tmp = env
    con = db.connect(config.load("config.toml").db_path)
    db.add(con, Clip(id="twitch:a", platform="twitch", creator="x", credit="x", title="t", url="u", status="rendered"))
    assert post(client, "/api/clips/twitch:a/approve").status_code == 200
    assert post(client, "/api/clips/twitch:a/approve").status_code == 400
    assert db.get(con, "twitch:a").status == "approved"


def test_rediscovery_refreshes_score_of_waiting_clips(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    clip = Clip(id="twitch:a", platform="twitch", creator="x", credit="x", title="t", url="u", views=60, score=1.0)
    db.add(con, clip)
    clip.views, clip.score = 50000, 900.0
    assert db.add(con, clip) is False
    assert db.get(con, "twitch:a").views == 50000 and db.get(con, "twitch:a").score == 900.0


def test_publish_posts_to_every_connected_account_and_retries_only_failures(tmp_path, monkeypatch):
    cfg = config.Config(data_dir=tmp_path)
    con = db.connect(cfg.db_path)
    db.add(con, Clip(id="c", platform="twitch", creator="x", credit="x", title="t", url="u", status="approved"))
    calls = []

    class Fake:
        def __init__(self, name, fail):
            self.name, self.fail = name, fail
        def upload(self, cfg, clip):
            calls.append(self.name)
            if self.fail:
                raise RuntimeError("boom")
            return f"https://{self.name}/1"

    yt, tt = Fake("youtube", False), Fake("tiktok", True)
    monkeypatch.setattr(publish, "PLATFORMS", {"youtube": yt, "tiktok": tt})
    monkeypatch.setattr(publish, "connected", lambda cfg: ["youtube", "tiktok"])
    pipeline.publish_approved(cfg, con, log=lambda m: None)
    assert db.get(con, "c").status == "approved"          # tiktok failed, so not finished
    tt.fail = False
    pipeline.publish_approved(cfg, con, log=lambda m: None)
    assert calls == ["youtube", "tiktok", "tiktok"]       # youtube was not posted twice
    assert db.get(con, "c").status == "published"


def test_tiktok_chunk_plan():
    assert tiktok.chunk_plan(3_000_000) == (3_000_000, 1)
    size = 100 * 1024 * 1024 + 5
    chunk, count = tiktok.chunk_plan(size)
    assert count == 10 and chunk * count <= size


def test_layout_change_validates_and_upload_is_origin_checked(env):
    import io
    client, tmp = env
    con = db.connect(config.load("config.toml").db_path)
    db.add(con, Clip(id="twitch:a", platform="twitch", creator="x", credit="x", title="t", url="u", status="rendered"))
    assert post(client, "/api/clips/twitch:a/layout", {"layout": "nope"}).status_code == 400
    r = post(client, "/api/clips/twitch:a/layout", {"layout": "split"})
    assert r.status_code == 400 and "gameplay" in r.get_json()["error"]

    def upload(name, origin):
        return client.post("/api/backgrounds", data={"file": (io.BytesIO(b"x"), name)},
                           content_type="multipart/form-data", headers={"Origin": origin})
    assert upload("game.mp4", "http://evil.example").status_code == 403
    assert upload("notes.txt", "http://127.0.0.1:8777").status_code == 400
    assert upload("../../game.mp4", "http://127.0.0.1:8777").status_code == 200
    assert client.get("/api/state").get_json()["backgrounds"] == ["game.mp4"]
    assert (tmp / "data" / "backgrounds" / "game.mp4").exists()


def test_all_content_streamers_skip_the_game_filter_and_get_their_own_tags(monkeypatch):
    from datetime import datetime, timezone
    from clipz.config import Config, Creator
    from clipz.publish import youtube
    from clipz.sources import twitch

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    def fake_get(s, path, **params):
        if path == "games":
            return [{"id": "GTA"}]
        if path == "users":
            return [{"login": "gamer", "id": "1"}, {"login": "irl", "id": "2"}]
        return [{"id": f"{params['broadcaster_id']}-{g}", "game_id": g, "view_count": 999, "title": "t",
                 "url": "u", "created_at": now, "duration": 20} for g in ("GTA", "IRL")]
    monkeypatch.setattr(twitch, "_session", lambda cfg: None)
    monkeypatch.setattr(twitch, "_get", fake_get)
    cfg = Config(creators=[Creator("twitch", "gamer"), Creator("twitch", "irl", all_content=True, hashtags=["#togi"])],
                 render={"hashtags": ["GTA6"]})
    clips = {c.id: c for c in twitch.discover(cfg, cfg.creators)}
    assert sorted(clips) == ["twitch:1-GTA", "twitch:2-GTA", "twitch:2-IRL"]
    irl, game = clips["twitch:2-IRL"], clips["twitch:1-GTA"]
    assert cfg.hashtags_for(irl) == ["togi"] and cfg.hashtags_for(game) == ["GTA6"]
    assert youtube.metadata(irl, cfg.hashtags_for(irl), cfg.is_gaming(irl))["categoryId"] == "24"
    assert youtube.metadata(game, cfg.hashtags_for(game), cfg.is_gaming(game))["categoryId"] == "20"


def test_instagram_connect_and_reel_upload_sequence(tmp_path, monkeypatch):
    from clipz.publish import instagram
    cfg = config.Config(data_dir=tmp_path)
    video = tmp_path / "v.mp4"; video.write_bytes(b"12345")
    clip = Clip(id="c", platform="twitch", creator="x", credit="twitch.tv/x", title="t", url="u", out_path=str(video))
    calls, polls = [], iter(["IN_PROGRESS", "FINISHED"])

    class Reply:
        def __init__(self, data, code=200): self.data, self.status_code = data, code
        def json(self): return self.data

    def get(url, params=None, **kw):
        calls.append(("GET", url))
        if url.endswith("/me"):
            return Reply({"error": {"message": "bad token"}}, 400) if params["access_token"] == "bad" else Reply({"user_id": 42, "username": "me"})
        if url.endswith("/CONT"):
            return Reply({"status_code": next(polls)})
        return Reply({"permalink": "https://instagram.com/reel/abc"})

    def post(url, data=None, headers=None, **kw):
        calls.append(("POST", url))
        if url.endswith("/42/media"):
            assert data["media_type"] == "REELS" and "twitch.tv/x" in data["caption"]
            return Reply({"id": "CONT"})
        if "rupload" in url:
            assert headers["file_size"] == "5" and data.read() == b"12345"
            return Reply({"success": True})
        assert data["creation_id"] == "CONT"
        return Reply({"id": "MEDIA"})

    monkeypatch.setattr(instagram.requests, "get", get)
    monkeypatch.setattr(instagram.requests, "post", post)
    with pytest.raises(RuntimeError, match="bad token"):
        instagram.connect(cfg, "bad")
    assert not instagram.connected(cfg)
    assert instagram.connect(cfg, " good ") == "me" and instagram.connected(cfg)
    assert instagram.upload(cfg, clip, sleep=lambda s: None) == "https://instagram.com/reel/abc"
    order = [u.rsplit("/", 1)[-1] for m, u in calls if m == "POST"]
    assert order == ["media", "CONT", "media_publish"]
