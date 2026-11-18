"""Web app: a few buttons on top of the same pipeline the command line uses.

Three ways to run it:
- On your own computer (no password): only this machine's browser is let in.
- Hosted on a server (`clipz app --host 0.0.0.0`): a password is required.
- On Vercel (see vercel.json and ../app.py): a password is required, and background work runs
  as short worker requests instead of threads, because a cloud function stops when it replies.
The app holds your API keys and can post to your accounts, so it never runs open to the internet.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
import webbrowser
import zlib
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import requests
from flask import Flask, abort, g, jsonify, redirect, request, send_file, session
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.routing import PathConverter
from werkzeug.utils import secure_filename

from . import config, db, download, files, pipeline, publish, rank, render, worker
from .config import Creator
from .publish import instagram, tiktok, youtube
from .sources import kick as kick_source, twitch as twitch_source, youtube as youtube_source

SECRET_FIELDS = {"twitch": ["client_secret"], "youtube_upload": ["client_secret"], "tiktok": ["client_secret"]}
STATIC = Path(__file__).parent / "static"
CLOUD_BUDGET = 120      # seconds of work per cloud worker run, leaving room under the 300s limit
WAKE_COOLDOWN = 30      # don't re-wake a stalled worker more often than this
AUTO_FIND_EVERY = 300   # look for new clips when the app is open and the last search is older than this

LOGIN_PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width, initial-scale=1">
<title>Clipz</title><link rel=manifest href=/static/manifest.webmanifest>
<link rel=apple-touch-icon href=/static/icon-192.png><meta name=apple-mobile-web-app-capable content=yes><style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0f1115;color:#e8eaf0;
font:16px system-ui,sans-serif}form{display:grid;gap:12px;width:min(320px,calc(100vw - 32px))}input,button{font:inherit;
padding:12px;border-radius:8px;border:1px solid #2a2f3a;background:#181b22;color:inherit}button{background:#7c5cff;
border-color:#7c5cff;font-weight:600}p{color:#e5484d;margin:0}</style>
<form method=post><h1 style="margin:0">Clipz</h1>__ERROR__
<input type=password name=password placeholder=Password autofocus required><button>Sign in</button></form>"""



VIEW_RANGES = ("0-100", "100-1000", "1000-10000", "10000-100000", "100000-")


def view_range(text: str) -> tuple[float, float]:
    """"100-1000" -> (100, 1000); "100000-" -> (100000, inf); "" -> everything."""
    low, _, high = (text or "").partition("-")
    try:
        return float(low or 0), float(high) if high else float("inf")
    except ValueError:
        return 0.0, float("inf")


class ClipIdConverter(PathConverter):
    """Clip ids contain ":" (e.g. "twitch:AbC"), which the page sends as "%3A". Vercel hands
    the path over still encoded, so decode it here or no clip would ever be found."""
    def to_python(self, value: str) -> str:
        return unquote(value)


def create_app(config_path: str = "config.toml", port: int = 8777, password: str = "",
               cloud: bool | None = None, wake=None) -> Flask:
    """cloud: run background work as worker requests (Vercel). wake: override how a worker is
    started (tests)."""
    cloud = bool(os.environ.get("VERCEL")) if cloud is None else cloud
    app = Flask(__name__, static_folder=None)
    app.url_map.converters["clip"] = ClipIdConverter
    if password or cloud:
        # Behind the host's HTTPS proxy. The session key is derived from the password so every
        # cloud instance agrees on it, and changing the password signs everyone out.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
        app.secret_key = hashlib.sha256(b"clipz-session-v1:" + password.encode()).hexdigest()
        app.permanent_session_lifetime = timedelta(days=30)
        app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                          SESSION_COOKIE_SECURE=cloud)
    else:
        app.secret_key = secrets.token_hex(32)  # local: one process, used only for sign-in round trips
    worker_token = os.environ.get("CRON_SECRET") or hmac.new(
        (password or app.secret_key).encode(), b"clipz-worker", "sha256").hexdigest()
    last_auto_run = [time.time()]

    def cfg() -> config.Config:
        """This request's settings and database connection (loaded once per request)."""
        if "cfg" not in g:
            g.cfg = config.load(config_path)
        return g.cfg

    @app.teardown_appcontext
    def close_db(_exc):
        c = g.pop("cfg", None)
        if c is not None and c._con is not None:
            c._con.close()

    def say(msg: str) -> None:
        db.log_append(cfg().connect(), msg)
        print(msg, flush=True)  # also into the host's logs, for troubleshooting

    def run_local_worker() -> None:
        c = config.load(config_path)  # a thread needs its own connection
        try:
            worker.run(c, lambda m: (db.log_append(c.connect(), m), print(m, flush=True)))
        except Exception as e:
            db.log_append(c.connect(), f"Background work FAILED: {e}")
        finally:
            c.connect().close()

    def start_worker() -> None:
        db.kv_set(cfg().connect(), "last_wake", time.time())
        if wake is not None:
            wake()
        elif cloud:
            # Fire and forget: the new request carries on after we stop waiting for it.
            try:
                requests.post(f"{request.url_root.rstrip('/')}/api/worker", timeout=(5, 1.5),
                              headers={"Authorization": f"Bearer {worker_token}"})
            except requests.RequestException:
                pass
        else:
            threading.Thread(target=run_local_worker, daemon=True).start()

    def scheduler() -> None:
        """Local only: search every N minutes while the app is open."""
        while True:
            time.sleep(20)
            try:
                c = config.load(config_path)
                minutes = c.schedule_minutes
                if minutes > 0 and time.time() - last_auto_run[0] >= minutes * 60:
                    last_auto_run[0] = time.time()
                    worker.request_find(c)
                    threading.Thread(target=run_local_worker, daemon=True).start()
                c.connect().close()
            except Exception:
                continue

    def worker_authorised() -> bool:
        return hmac.compare_digest(request.headers.get("Authorization", "").encode(),
                                   f"Bearer {worker_token}".encode())

    @app.before_request
    def guard():
        if request.endpoint in ("worker_run", "cron"):
            if not worker_authorised():
                abort(403)
            return
        if request.endpoint == "static_file":
            return  # the home-screen icon must load before signing in
        if password:
            if request.endpoint == "login":
                return
            if not session.get("ok"):
                return (jsonify(error="Signed out - reload the page and sign in again."), 401) \
                    if request.path.startswith("/api/") else redirect("/login")
        elif cloud:
            abort(403)  # never run open to the internet
        elif request.host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            abort(403)  # no password set: only this machine's own browser may talk to the app
        if request.method == "GET":
            return
        # Only our own page can attach this header, so other websites can't press our buttons.
        if request.headers.get("X-Clipz") != "1":
            abort(403)
        if request.endpoint != "upload_background" and not request.is_json:
            abort(415)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not password:
            return redirect("/")
        error = ""
        if request.method == "POST":
            if hmac.compare_digest(request.form.get("password", "").encode(), password.encode()):
                session.permanent = True
                session["ok"] = True
                return redirect("/")
            time.sleep(1)  # slow down password guessing
            error = "<p>Wrong password.</p>"
        return LOGIN_PAGE.replace("__ERROR__", error)

    @app.get("/static/<name>")
    def static_file(name):
        allowed = {"manifest.webmanifest": "application/manifest+json", "icon-192.png": "image/png", "icon-512.png": "image/png"}
        if name not in allowed:
            abort(404)
        return send_file(STATIC / name, mimetype=allowed[name])

    @app.get("/")
    def index():
        return send_file(STATIC / "index.html", max_age=0)  # always the latest version after an update

    def media_url(clip: db.Clip) -> str:
        if files.is_url(clip.out_path):
            return clip.out_path
        out = Path(clip.out_path)
        version = int(out.stat().st_mtime) if out.exists() else 0
        return f"/media/{quote(clip.id, safe='')}?v={version}"

    @app.get("/api/state")
    def get_state():
        c = cfg()
        con = c.connect()
        job = worker.busy(c)
        if (not job and (c.creators or c.games)
                and time.time() - db.kv_get(con, "last_find", 0) > AUTO_FIND_EVERY):
            worker.request_find(c)   # opening the app (or keeping it open) keeps the feed fresh
            start_worker()
            job = "Finding clips"
        elif not job and worker.pending(c) and time.time() - db.kv_get(con, "last_wake", 0) > WAKE_COOLDOWN:
            start_worker()  # a hand-off between workers was lost: pick the work back up
            job = "Starting"
        clips = []
        for clip in db.all_clips(con):
            if clip.status in ("discovered", "downloaded"):
                continue
            clips.append({**asdict(clip), "posts": db.posts(con, clip.id),
                          "layout": clip.layout or c.render.get("layout", "framed"),
                          "media_url": media_url(clip) if clip.out_path else "",
                          "version": zlib.crc32(media_url(clip).encode()) if clip.out_path else 0})
        settings = {k: getattr(c, k) for k in config.SCALARS}
        settings["keywords"] = c.keywords
        settings["hashtags"] = c.render.get("hashtags", [])
        settings["layout"] = c.render.get("layout", "framed")
        for section in ("twitch", "youtube_upload", "tiktok"):
            values = dict(getattr(c, section))
            for secret in SECRET_FIELDS[section]:
                values[secret + "_set"] = bool(values.pop(secret, ""))
            settings[section] = values
        return jsonify(
            creators=[asdict(x) for x in c.creators], clips=clips,
            categories=sorted({x.category for x in c.creators} | {g.get("label") or g["name"] for g in c.games}),
            games=c.games, muted=c.muted or {},
            waiting=len(db.by_status(con, "discovered")),
            settings=settings,
            accounts={**{n: m.connected(c) for n, m in publish.PLATFORMS.items()},
                      "twitch": bool(c.twitch.get("client_id") and c.twitch.get("client_secret")),
                      "ytcookies": bool(db.kv_get(con, "youtube_cookies"))},
            layouts=render.LAYOUTS, backgrounds=pipeline.backgrounds(c),
            tiktok_redirect=redirect_uri("tiktok"), youtube_redirect=redirect_uri("youtube"),
            hosted=bool(password), cloud=cloud, job=job, log=db.log_tail(con),
        )

    @app.post("/api/creators")
    def add_creator():
        body = request.get_json()
        try:
            new = Creator(body.get("platform", ""), body.get("channel", ""), body.get("credit", ""),
                          bool(body.get("all_content")), list(body.get("hashtags") or []),
                          body.get("category", ""), body.get("name", ""))
        except ValueError as e:
            return jsonify(error=str(e)), 400
        c = cfg()
        if any(x.platform == new.platform and x.channel.lower() == new.channel.lower() for x in c.creators):
            return jsonify(error="That streamer is already on your list."), 400
        config.save_settings(c, {"creators": [asdict(x) for x in [*c.creators, new]]})
        return jsonify(ok=True)

    @app.get("/api/search/streamers")
    def search_streamers():
        """Suggestions while typing a channel name."""
        c, platform = cfg(), request.args.get("platform")
        searchers = {"twitch": twitch_source, "kick": kick_source, "youtube": youtube_source}
        if platform not in searchers:
            return jsonify(results=[])
        if platform == "twitch" and not (c.twitch.get("client_id") and c.twitch.get("client_secret")):
            return jsonify(results=[], error="Connect Twitch in Settings to get suggestions.")
        try:
            return jsonify(results=searchers[platform].search_channels(c, request.args.get("q", "")))
        except Exception as e:
            return jsonify(results=[], error=f"Couldn't search {platform.title()} right now - type the exact channel name. ({str(e)[:120]})")

    @app.get("/api/search/games")
    def search_games():
        c = cfg()
        if not (c.twitch.get("client_id") and c.twitch.get("client_secret")):
            return jsonify(results=[], error="Connect Twitch in Settings to search games.")
        try:
            return jsonify(results=twitch_source.search_categories(c, request.args.get("q", "")))
        except Exception as e:
            return jsonify(results=[], error=f"Couldn't search games right now. ({str(e)[:120]})")

    @app.post("/api/games")
    def add_game():
        body, c = request.get_json(), cfg()
        name = (body.get("name") or "").strip()
        if not name:
            return jsonify(error="Pick a game from the list."), 400
        if any(g["name"].lower() == name.lower() for g in c.games):
            return jsonify(error="You're already following that game."), 400
        kick_matches = kick_source.search_categories(name)
        kick_slug = next((k["slug"] for k in kick_matches if k["name"].lower() == name.lower()),
                         kick_matches[0]["slug"] if kick_matches else kick_source.slugify(name))
        game = {"name": name, "twitch_id": str(body.get("id") or ""), "image": body.get("image") or "",
                "label": (body.get("label") or "").strip() or name, "kick_slug": kick_slug}
        config.save_settings(c, {"games": [*c.games, game]})
        say(f"Following {game['label']}. Press Find new clips to get its top clips.")
        return jsonify(ok=True)

    @app.delete("/api/games")
    def remove_game():
        name, c = request.get_json().get("name", ""), cfg()
        config.save_settings(c, {"games": [g for g in c.games if g["name"] != name]})
        return jsonify(ok=True)

    @app.post("/api/creators/toggle")
    def toggle_creator():
        """Switch a streamer between all their clips and the game-only filter."""
        body, c = request.get_json(), cfg()
        creators = [asdict(x) for x in c.creators]
        for x in creators:
            if x["platform"] == body.get("platform") and x["channel"] == body.get("channel"):
                x["all_content"] = not x["all_content"]
        config.save_settings(c, {"creators": creators})
        return jsonify(ok=True)

    def creator_key(clip) -> str:
        return f"{clip.platform}:{clip.creator.lower()}"

    def feed_items(c, con):
        """New finds for the feed: not yet saved or hidden, not muted. Unseen first, then best first."""
        muted = c.muted or {}
        hidden_creators = set(muted.get("creators", []))
        hidden_games = {g.lower() for g in muted.get("games", [])}
        followed_games = {(g.get("label") or g["name"]).lower() for g in c.games}
        items = []
        for clip in db.by_status(con, "discovered"):
            if not c.wanted_language(clip):
                continue
            creator = c.creator_for(clip)
            from_game = bool(clip.category) and clip.category.lower() in followed_games
            if not creator and not from_game:
                continue   # its streamer or game was unfollowed
            game = clip.game or (clip.category if clip.category != "General" else "")
            if creator_key(clip) in hidden_creators or (game and game.lower() in hidden_games):
                continue
            items.append({**asdict(clip), "category": c.category_of(clip), "game": game, "key": creator_key(clip),
                          "followed": bool(creator),
                          "viral": rank.is_viral(clip),
                          "streamer": (creator.name or creator.channel) if creator else clip.creator})
        items.sort(key=lambda x: (x["seen"] == "1", not x["viral"], -(x["heat"] or 0), -x["score"]))
        return items

    @app.post("/api/examples")
    def add_example():
        """Learn from an example clip: look up a YouTube/TikTok link's title, channel and picture
        (their public oEmbed info) and remember it."""
        url = (request.get_json().get("url") or "").strip()
        host = urlparse(url).netloc.lower()
        if "youtube.com" in host or "youtu.be" in host:
            api_url = "https://www.youtube.com/oembed"
        elif "tiktok.com" in host:
            api_url = "https://www.tiktok.com/oembed"
        else:
            return jsonify(error="Paste a YouTube or TikTok link."), 400
        clean = url.split("?")[0] if "youtube.com/shorts/" in url else url
        try:
            r = requests.get(api_url, params={"url": clean, "format": "json"}, timeout=15)
            r.raise_for_status()
            info = r.json()
        except Exception as e:
            return jsonify(error=f"Couldn't read that link ({str(e)[:120]})."), 400
        c = cfg()
        example = {"url": clean, "title": info.get("title", ""), "channel": info.get("author_name", ""),
                   "channel_url": info.get("author_url", ""), "image": info.get("thumbnail_url", ""),
                   "platform": "tiktok" if "tiktok" in api_url else "youtube",
                   "game": c.infer_game(info.get("title", "")), "at": time.time()}
        examples = [e for e in db.kv_get(c.connect(), "examples", []) if e["url"] != clean][-19:] + [example]
        db.kv_set(c.connect(), "examples", examples)
        print(f"EXAMPLE CLIP: {example}", flush=True)   # so the clip style can be reviewed in the logs
        return jsonify(example=example)

    @app.get("/api/similar")
    def similar():
        """Streamers like the ones you follow: live in the games your streamers' clips are from."""
        c = cfg()
        if not (c.twitch.get("client_id") and c.twitch.get("client_secret")) or not c.creators:
            return jsonify(results=[])
        con = c.connect()
        followed = {(x.platform, x.channel.lower()) for x in c.creators}
        counts = {}
        for clip in db.all_clips(con):
            if (clip.platform, clip.creator.lower()) in followed and clip.game:
                counts[clip.game] = counts.get(clip.game, 0) + 1
        label_to_name = {(g.get("label") or g["name"]): g["name"] for g in c.games}
        games = [label_to_name.get(g, g) for g, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:4]]
        if not games:
            games = [g["name"] for g in c.games[:4]]
        exclude = {ch for p, ch in followed if p == "twitch"}
        key = "|".join(games) + "#" + ",".join(sorted(exclude))
        cached = db.kv_get(con, "similar")
        if cached and cached.get("key") == key and time.time() - cached["at"] < 1800:
            return jsonify(results=cached["results"])
        try:
            results = twitch_source.similar_streamers(c, games, exclude)
        except Exception as e:
            return jsonify(results=[], error=str(e)[:200])
        db.kv_set(con, "similar", {"key": key, "at": time.time(), "results": results})
        return jsonify(results=results)

    @app.get("/api/feed")
    @app.get("/api/browse")
    def feed():
        """The swipe feed. ?game= and ?creator= (platform:name) narrow it down; ?category= too."""
        c = cfg()
        con = c.connect()
        game, creator, category = (request.args.get(k, "") for k in ("game", "creator", "category"))
        low, high = view_range(request.args.get("views", ""))
        offset, limit = int(request.args.get("offset", 0)), min(int(request.args.get("limit", 20)), 50)
        items = [x for x in feed_items(c, con)
                 if (not game or x["game"].lower() == game.lower())
                 and (not creator or x["key"] == creator.lower() or (creator == "following" and x["followed"]))
                 and (not category or x["category"] == category)
                 and low <= (x["views"] or 0) < high
                 and (request.args.get("viral") != "1" or x["viral"])]
        return jsonify(clips=items[offset:offset + limit], total=len(items))

    @app.get("/api/feed/filters")
    def feed_filters():
        """What the feed can be narrowed to: games, creators and view ranges with clips waiting."""
        c = cfg()
        items = feed_items(c, c.connect())
        views = [{"range": r, "count": sum(1 for x in items if view_range(r)[0] <= (x["views"] or 0) < view_range(r)[1])}
                 for r in VIEW_RANGES]
        viral = sum(1 for x in items if x["viral"])
        games, creators = {}, {}
        for x in items:
            if x["game"]:
                games[x["game"]] = games.get(x["game"], 0) + 1
            entry = creators.setdefault(x["key"], {"key": x["key"], "name": x["streamer"], "platform": x["platform"], "count": 0})
            entry["count"] += 1
        muted = c.muted or {}
        hidden_games = {x.lower() for x in muted.get("games", [])}
        for followed in c.games:
            label = followed.get("label") or followed["name"]
            if label.lower() not in hidden_games:
                games.setdefault(label, 0)
        for cr in c.creators:
            key = f"{cr.platform}:{cr.channel.lower()}"
            if key not in muted.get("creators", []):
                creators.setdefault(key, {"key": key, "name": cr.name or cr.channel, "platform": cr.platform, "count": 0})
        return jsonify(
            games=[{"name": n, "count": k} for n, k in sorted(games.items(), key=lambda kv: (-kv[1], kv[0].lower()))],
            creators=sorted(creators.values(), key=lambda x: (-x["count"], x["name"].lower()))[:60],
            following=sum(1 for x in items if x["followed"]),
            views=views, viral=viral)

    @app.get("/api/clips/<clip:clip_id>/stream")
    def clip_stream(clip_id):
        """A direct video address the feed can autoplay in its own player. Twitch's embedded
        player won't start by itself on phones; a plain muted <video> will."""
        c = cfg()
        con = c.connect()
        clip = db.get(con, clip_id)
        if not clip:
            abort(404)
        if clip.platform == "kick" and clip.preview:
            return jsonify(url=clip.preview)
        if clip.platform != "twitch":
            return jsonify(url="")             # YouTube: its own player (Vercel can't fetch its videos)
        cached = db.kv_get(con, f"stream:{clip_id}")
        if cached and time.time() - cached["at"] < 3600:
            return jsonify(url=cached["url"])
        try:
            url = download.stream_url(clip.url)
        except Exception as e:
            return jsonify(url="", error=str(e)[:200])
        db.kv_set(con, f"stream:{clip_id}", {"url": url, "at": time.time()})
        return jsonify(url=url)

    @app.post("/api/clips/seen")
    def mark_seen():
        con = cfg().connect()
        for clip_id in (request.get_json().get("ids") or [])[:50]:
            try:
                db.update(con, clip_id, seen="1")
            except KeyError:
                pass
        return jsonify(ok=True)

    @app.post("/api/mute")
    def mute():
        """Don't show more like this: hide a creator's or a game's clips from the feed."""
        body, c = request.get_json(), cfg()
        kind, value = body.get("kind"), (body.get("value") or "").strip()
        if kind not in ("creator", "game") or not value:
            return jsonify(error="Nothing to hide."), 400
        muted = dict(c.muted or {})
        key = kind + "s"
        muted[key] = sorted(set(muted.get(key, [])) | {value.lower() if kind == "creator" else value})
        labels = dict(muted.get("labels", {}))
        labels[value.lower() if kind == "creator" else value] = body.get("label") or value
        muted["labels"] = labels
        config.save_settings(c, {"muted": muted})
        return jsonify(ok=True)

    @app.delete("/api/mute")
    def unmute():
        body, c = request.get_json(), cfg()
        muted = dict(c.muted or {})
        key, value = (body.get("kind") or "") + "s", body.get("value") or ""
        muted[key] = [v for v in muted.get(key, []) if v != value]
        config.save_settings(c, {"muted": muted})
        return jsonify(ok=True)

    @app.delete("/api/creators")
    def remove_creator():
        body = request.get_json()
        c = cfg()
        keep = [asdict(x) for x in c.creators
                if not (x.platform == body.get("platform") and x.channel == body.get("channel"))]
        config.save_settings(c, {"creators": keep})
        return jsonify(ok=True)

    @app.post("/api/settings")
    def save():
        body, changes = request.get_json(), {}
        for key, kind in config.SCALARS.items():
            if key in body:
                try:
                    changes[key] = kind(body[key])
                except (TypeError, ValueError):
                    return jsonify(error=f"{key} must be a {kind.__name__}"), 400
        if "keywords" in body:
            changes["keywords"] = [k.strip() for k in body["keywords"] if k.strip()]
        if "hashtags" in body:
            changes.setdefault("render", {})["hashtags"] = [h.strip().lstrip("#") for h in body["hashtags"] if h.strip()]
        if "layout" in body:
            if body["layout"] not in render.LAYOUTS:
                return jsonify(error="unknown layout"), 400
            changes.setdefault("render", {})["layout"] = body["layout"]
        for section in ("twitch", "youtube_upload", "tiktok"):
            # Blank fields mean "leave as is" so saved secrets never have to be re-typed.
            values = {k: v.strip() for k, v in (body.get(section) or {}).items() if isinstance(v, str) and v.strip()}
            if values:
                changes[section] = values
        config.save_settings(cfg(), changes)
        return jsonify(ok=True)

    @app.post("/api/run")
    def run():
        worker.request_find(cfg())
        start_worker()
        return jsonify(started=True)

    @app.post("/api/publish")
    def post_all():
        worker.request_publish(cfg())
        start_worker()
        return jsonify(started=True)

    @app.post("/api/retry")
    def retry():
        con = cfg().connect()
        for clip in db.by_status(con, "failed"):
            db.update(con, clip.id, status="queued")
        start_worker()
        return jsonify(started=True)

    def format_choice(c, body, clip):
        """Optional layout/headline sent with Post or Save. Returns (fields, error)."""
        fields = {}
        if body.get("layout"):
            if body["layout"] not in render.LAYOUTS:
                return None, "unknown layout"
            if body["layout"] == "split" and not pipeline.backgrounds(c):
                return None, "The Clip + gameplay format needs a gameplay video - add one in the Publishing tab, or pick another format."
            fields["layout"] = body["layout"]
        if (body.get("title") or "").strip():
            fields["title"] = body["title"].strip()[:200]
        if "spoiler" in body:
            fields["spoiler"] = "1" if body["spoiler"] else ""
        return fields, None

    @app.get("/api/scoreboard.csv")
    def scoreboard():
        """Every post that went out, in the page's scoreboard columns. Metrics stay blank
        for you to fill at 12h/24h/72h - never guessed."""
        import csv, io
        from datetime import datetime as _dt
        con = cfg().connect()
        cols = ["post_id", "date", "time", "platform", "pillar", "format", "hook", "length_sec", "credit",
                "views_12h", "views_24h", "views_72h", "likes", "reposts_or_shares", "replies_or_comments",
                "saves_or_bookmarks", "followers_at_read", "test_name", "notes"]
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(cols)
        for p in db.all_posts(con):
            clip = db.get(con, p["clip_id"])
            if not clip:
                continue
            when = _dt.fromtimestamp(float(p["at"] or 0)).astimezone() if p["at"] else None
            w.writerow([p["ref"], when.strftime("%Y-%m-%d") if when else "", when.strftime("%H:%M") if when else "",
                        p["platform"], "Gameplay clips", clip.layout or c_layout(),
                        db.hook(clip), round(min(clip.duration or 0, cfg().max_duration)) or "", db.credit_tag(clip)]
                       + [""] * 10)
        return app.response_class(out.getvalue(), mimetype="text/csv",
                                  headers={"Content-Disposition": "attachment; filename=scoreboard.csv"})

    def c_layout():
        return cfg().render.get("layout", "framed")

    @app.post("/api/clips/<clip:clip_id>/post")
    def post_clip(clip_id):
        """Post this clip: edit it in the chosen format if needed, then post to every connected account."""
        body, c = request.get_json(), cfg()
        con = c.connect()
        clip = db.get(con, clip_id)
        if not clip:
            abort(404)
        fields, error = format_choice(c, body, clip)
        if error:
            return jsonify(error=error), 400
        reformat = bool(fields) and clip.status in ("rendered", "approved", "rejected")
        if clip.status in ("discovered", "skipped", "failed") or reformat:
            db.update(con, clip_id, status="queued", approve="1", autopost="1", **fields)
        elif clip.status in ("rendered", "approved", "rejected"):
            db.update(con, clip_id, status="approved", **fields)
            worker.request_publish(c)
        else:
            return jsonify(error=f"clip is {clip.status}"), 400
        start_worker()
        return jsonify(ok=True, accounts=publish.connected(c))

    @app.post("/api/clips/<clip:clip_id>/keep")
    def keep_clip(clip_id):
        """Save for later: edit it in the chosen format and leave it in Publishing to post later."""
        body, c = request.get_json(), cfg()
        con = c.connect()
        clip = db.get(con, clip_id)
        if not clip or clip.status not in ("discovered", "skipped", "failed"):
            return jsonify(error="That clip can't be saved now."), 400
        fields, error = format_choice(c, body, clip)
        if error:
            return jsonify(error=error), 400
        db.update(con, clip_id, status="queued", approve="", autopost="", **fields)
        start_worker()
        return jsonify(ok=True)

    @app.post("/api/clips/<clip:clip_id>/layout")
    def set_layout(clip_id):
        """Change a clip's layout / gameplay video / headline and re-edit it."""
        body = request.get_json()
        c = cfg()
        con = c.connect()
        clip = db.get(con, clip_id)
        if not clip:
            abort(404)
        if clip.status not in ("rendered", "approved", "rejected", "failed"):
            return jsonify(error=f"clip is {clip.status} and can't be re-edited"), 400
        layout, background = body.get("layout", ""), body.get("background", "")
        if layout not in render.LAYOUTS:
            return jsonify(error="unknown layout"), 400
        if layout == "split":
            available = pipeline.backgrounds(c)
            if not available:
                return jsonify(error="Add a gameplay video first - the split layout needs one."), 400
            if background and background not in available:
                return jsonify(error="That gameplay video no longer exists."), 400
        title = (body.get("title") or clip.title).strip()
        db.update(con, clip_id, layout=layout, background=background, title=title, status="queued")
        start_worker()
        return jsonify(started=True)

    @app.post("/api/backgrounds")
    def upload_background():
        f = request.files.get("file")
        name = secure_filename(f.filename) if f and f.filename else ""
        if not name or Path(name).suffix.lower() not in render.BACKGROUND_TYPES:
            return jsonify(error="Choose a video file (" + ", ".join(render.BACKGROUND_TYPES) + ")."), 400
        c = cfg()
        folder = c.data_dir / "backgrounds"
        folder.mkdir(parents=True, exist_ok=True)
        f.save(folder / name)
        files.add_background(c, folder / name, name)
        say(f"Added gameplay video {name}")
        return jsonify(ok=True, name=name)

    @app.post("/api/backgrounds/link")
    def background_from_link():
        """Gameplay video from a link - the way to add one from a phone, since cloud functions
        can't accept large uploads."""
        url = (request.get_json().get("url") or "").strip()
        if urlparse(url).scheme not in ("http", "https"):
            return jsonify(error="Paste a full link starting with https://"), 400
        c = cfg()
        name = secure_filename(Path(urlparse(url).path).stem)[:40] or "gameplay"
        name = f"{name}-{secrets.token_hex(2)}.mp4"
        try:
            path = download.fetch_video(url, c.data_dir / "backgrounds", name)
        except Exception as e:
            return jsonify(error=f"Couldn't get a video from that link: {str(e)[:200]}"), 400
        files.add_background(c, path, name)
        say(f"Added gameplay video {name}")
        return jsonify(ok=True, name=name)

    @app.post("/api/clips/<clip:clip_id>/<action>")
    def set_status(clip_id, action):
        moves = {"approve": (("rendered", "rejected"), "approved"), "reject": (("rendered", "approved"), "rejected"),
                 "edit": (("discovered", "skipped", "failed"), "queued"), "skip": (("discovered",), "skipped"),
                 "save": (("discovered", "skipped", "failed"), "queued")}   # save for later
        if action not in moves:
            abort(404)
        con = cfg().connect()
        clip = db.get(con, clip_id)
        if not clip:
            abort(404)
        allowed, to = moves[action]
        if clip.status not in allowed:
            return jsonify(error=f"clip is already {clip.status}"), 400
        db.update(con, clip_id, status=to)
        if to == "queued":
            start_worker()
        return jsonify(ok=True)

    @app.get("/media/<clip:clip_id>")
    def media(clip_id):
        clip = db.get(cfg().connect(), clip_id)
        if not clip or not clip.out_path:
            abort(404)
        if files.is_url(clip.out_path):
            return redirect(clip.out_path)
        if not Path(clip.out_path).exists():
            abort(404)
        return send_file(Path(clip.out_path).resolve(), mimetype="video/mp4", conditional=True)

    def redirect_uri(platform: str) -> str:
        """Where a social platform sends the user back to - this app, at whatever address it's reached on."""
        return f"{request.url_root.rstrip('/')}/oauth/{platform}/callback"

    def begin_oauth(platform: str, module):
        try:
            url, st, verifier = module.authorize_url(cfg(), redirect_uri(platform))
        except RuntimeError as e:
            say(str(e))
            return redirect("/#accounts")
        session[f"oauth_{platform}"] = [st, verifier]  # signed cookie: works across cloud instances
        return redirect(url)

    def finish_oauth(platform: str, module, label: str):
        pending = session.pop(f"oauth_{platform}", None)
        if not pending or request.args.get("state") != pending[0] or "code" not in request.args:
            reason = request.args.get("error_description") or request.args.get("error") or "unexpected response"
            say(f"{label} connect failed: {reason}")
        else:
            try:
                module.finish_connect(cfg(), request.args["code"], pending[1], redirect_uri(platform))
                say(f"{label} connected.")
            except Exception as e:
                say(f"{label} connect failed: {e}")
        return redirect("/#accounts")

    @app.get("/connect/youtube")
    def connect_youtube():
        return begin_oauth("youtube", youtube)

    @app.get("/oauth/youtube/callback")
    def youtube_callback():
        return finish_oauth("youtube", youtube, "YouTube")

    @app.get("/connect/tiktok")
    def connect_tiktok():
        return begin_oauth("tiktok", tiktok)

    @app.get("/oauth/tiktok/callback")
    def tiktok_callback():
        return finish_oauth("tiktok", tiktok, "TikTok")

    @app.post("/api/connect/instagram")
    def connect_instagram():
        try:
            username = instagram.connect(cfg(), request.get_json().get("token", ""))
        except Exception as e:
            return jsonify(error=str(e)), 400
        say(f"Instagram connected{' as @' + username if username else ''}.")
        return jsonify(ok=True)

    @app.post("/api/connect/twitch")
    def connect_twitch():
        body = request.get_json()
        client_id, client_secret = (body.get("client_id") or "").strip(), (body.get("client_secret") or "").strip()
        if not client_id or not client_secret:
            return jsonify(error="Paste both the Client ID and the Client secret."), 400
        try:
            twitch_source.app_token(client_id, client_secret)
        except Exception as e:
            return jsonify(error=str(e)), 400
        config.save_settings(cfg(), {"twitch": {"client_id": client_id, "client_secret": client_secret}})
        say("Twitch connected.")
        return jsonify(ok=True)

    @app.post("/api/connect/youtube-cookies")
    def connect_youtube_cookies():
        try:
            cookies = download.clean_cookies(request.get_json().get("cookies", ""))
        except ValueError as e:
            return jsonify(error=str(e)), 400
        con = cfg().connect()
        warning = ""
        try:   # a quick check that YouTube accepts them from here
            download.stream_url("https://www.youtube.com/watch?v=jNQXAC9IVRw", cookies=cookies)
        except Exception as e:
            if "not a bot" in str(e) or "Sign in" in str(e):
                return jsonify(error="YouTube didn't accept this sign-in. Export the cookies again "
                                     "(from a private window, then close it) and paste them here."), 400
            warning = str(e)[:200]
        db.kv_set(con, "youtube_cookies", {"text": cookies, "at": time.time()})
        retried = 0
        for clip in db.by_status(con, "failed"):
            if clip.platform == "youtube":
                db.update(con, clip.id, status="queued", error="")
                retried += 1
        say("YouTube sign-in added." + (f" Retrying {retried} YouTube clip{'s' * (retried != 1)}." if retried else ""))
        if retried:
            start_worker()
        return jsonify(ok=True, warning=warning, retried=retried)

    @app.post("/api/disconnect/<name>")
    def disconnect(name):
        if name == "ytcookies":
            db.kv_delete(cfg().connect(), "youtube_cookies")
            say("YouTube sign-in removed.")
            return jsonify(ok=True)
        if name == "twitch":
            config.save_settings(cfg(), {"twitch": {"client_id": "", "client_secret": ""}})
            say("Twitch disconnected.")
            return jsonify(ok=True)
        if name not in publish.PLATFORMS:
            abort(404)
        publish.PLATFORMS[name].disconnect(cfg())
        return jsonify(ok=True)

    def run_worker_here():
        result = worker.run(cfg(), say, budget=CLOUD_BUDGET if cloud else None)
        if result == "more":
            start_worker()
        return jsonify(result=result)

    @app.post("/api/worker")
    def worker_run():
        return run_worker_here()

    @app.get("/api/cron")
    def cron():
        """Vercel calls this on the schedule in vercel.json."""
        c = cfg()
        say("Scheduled search starting.")
        worker.request_find(c)
        if c.auto_approve:
            worker.request_publish(c)
        return run_worker_here()

    app.start_scheduler = lambda: threading.Thread(target=scheduler, daemon=True).start()
    return app


SETUP_PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width, initial-scale=1">
<title>Clipz setup</title><style>body{margin:0;background:#0f1115;color:#e8eaf0;font:16px/1.5 system-ui,sans-serif}
main{max-width:560px;margin:0 auto;padding:24px 16px}li{margin:10px 0}code{background:#181b22;padding:1px 6px;
border-radius:6px}</style><main><h1>Clipz needs a few things</h1>
<p>In your Vercel project, add the items below, then redeploy (Deployments tab, the latest one, Redeploy).</p>
<ol>__ITEMS__</ol></main>"""


def create_vercel_app() -> Flask:
    """The app Vercel runs. Shows a setup checklist instead of the app until everything it
    needs is in place, so it never runs without a password or anywhere to keep its data."""
    missing = []
    if not os.environ.get("CLIPZ_PASSWORD"):
        missing.append("<b>Password</b>: Settings, Environment Variables, add <code>CLIPZ_PASSWORD</code> "
                       "with a long password of your choice.")
    if not config.database_url():
        missing.append("<b>Database</b>: Storage tab, Create Database, choose <b>Neon</b> (free), "
                       "and connect it to this project.")
    if not files.cloud():
        missing.append("<b>Video storage</b>: Storage tab, Create, choose <b>Blob</b> with <b>public</b> "
                       "access, and connect it to this project.")
    if missing:
        setup = Flask(__name__)
        page = SETUP_PAGE.replace("__ITEMS__", "".join(f"<li>{m}</li>" for m in missing))
        setup.add_url_rule("/", "setup", lambda: page)
        setup.add_url_rule("/<path:_any>", "setup_any", lambda _any: page)
        return setup
    return create_app(password=os.environ["CLIPZ_PASSWORD"], cloud=True)

def serve(config_path: str = "config.toml", port: int = 8777, open_browser: bool = True,
          host: str = "127.0.0.1", password: str = "") -> None:
    local = host in ("127.0.0.1", "localhost")
    if not local and not password:
        raise SystemExit("Refusing to start: reachable from other devices but no password set. "
                         "Set the CLIPZ_PASSWORD environment variable (or pass --password).")
    app = create_app(config_path, port, password)
    app.start_scheduler()
    print(f"Clipz is running on port {port}" + ("  (password required)" if password else "") + "  (Ctrl+C to stop)")
    if open_browser and local:
        threading.Timer(1.0, webbrowser.open, [f"http://127.0.0.1:{port}"]).start()
    app.run(host=host, port=port, threaded=True)
