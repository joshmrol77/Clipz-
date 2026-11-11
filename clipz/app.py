"""Web app: a few buttons on top of the same pipeline the command line uses.

Two ways to run it. On your own computer (no password): only this machine's browser is let in.
Hosted on a server so you can use it from a phone: a password is required, because the app
holds your API keys and can post to your accounts."""
from __future__ import annotations

import hmac
import secrets
import threading
import time
import webbrowser
from datetime import timedelta
from collections import deque
from dataclasses import asdict
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, request, send_file, session
from werkzeug.middleware.proxy_fix import ProxyFix

from werkzeug.utils import secure_filename

from . import config, db, pipeline, publish, render
from .config import Creator
from .publish import instagram, tiktok, youtube

SECRET_FIELDS = {"twitch": ["client_secret"], "youtube_upload": ["client_secret"], "tiktok": ["client_secret"]}
STATIC = Path(__file__).parent / "static"
LOGIN_PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width, initial-scale=1">
<title>Clipz</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0f1115;color:#e8eaf0;
font:16px system-ui,sans-serif}form{display:grid;gap:12px;width:min(320px,calc(100vw - 32px))}input,button{font:inherit;
padding:12px;border-radius:8px;border:1px solid #2a2f3a;background:#181b22;color:inherit}button{background:#7c5cff;
border-color:#7c5cff;font-weight:600}p{color:#e5484d;margin:0}</style>
<form method=post><h1 style="margin:0">Clipz</h1>__ERROR__
<input type=password name=password placeholder=Password autofocus required><button>Sign in</button></form>"""


def create_app(config_path: str = "config.toml", port: int = 8777, password: str = "") -> Flask:
    app = Flask(__name__, static_folder=None)
    if password:
        # Hosted: we sit behind the host's HTTPS proxy, and sign-in is remembered with a cookie.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
        key_file = config.load(config_path).data_dir / "secret_key"
        if not key_file.exists():
            key_file.write_text(secrets.token_hex(32))
        app.secret_key = key_file.read_text()
        app.permanent_session_lifetime = timedelta(days=30)
        app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
    log: deque[str] = deque(maxlen=200)
    busy = threading.Lock()
    state = {"job": "", "last_auto_run": time.time(), "tiktok_pending": None, "youtube_pending": None}

    def redirect_uri(platform: str) -> str:
        """Where a social platform sends the user back to - this app, at whatever address it's reached on."""
        return f"{request.url_root.rstrip('/')}/oauth/{platform}/callback"

    def cfg():
        return config.load(config_path)

    def say(msg: str) -> None:
        log.append(f"{time.strftime('%H:%M:%S')}  {msg}")

    def start_job(name: str, fn) -> bool:
        """Run fn(cfg, con) in the background. Only one job at a time."""
        if not busy.acquire(blocking=False):
            return False

        def work():
            state["job"] = name
            try:
                c = cfg()
                fn(c, db.connect(c.db_path))
            except Exception as e:
                say(f"{name} FAILED: {e}")
            finally:
                state["job"] = ""
                busy.release()

        threading.Thread(target=work, daemon=True).start()
        return True

    def find_job(c, con):
        pipeline.find_and_render(c, con, say)
        if c.auto_approve:
            pipeline.publish_approved(c, con, log=say)

    def scheduler():
        while True:
            time.sleep(20)
            try:
                minutes = cfg().schedule_minutes
            except Exception:
                continue
            if minutes > 0 and time.time() - state["last_auto_run"] >= minutes * 60:
                state["last_auto_run"] = time.time()
                start_job("Finding clips (scheduled)", find_job)

    @app.before_request
    def guard():
        if password:
            if request.endpoint == "login":
                return
            if not session.get("ok"):
                return (jsonify(error="Signed out - reload the page and sign in again."), 401) \
                    if request.path.startswith("/api/") else redirect("/login")
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

    @app.get("/")
    def index():
        return send_file(STATIC / "index.html")

    @app.get("/api/state")
    def get_state():
        c = cfg()
        con = db.connect(c.db_path)
        clips = []
        for clip in db.all_clips(con):
            if clip.status in ("discovered", "downloaded"):
                continue
            out = Path(clip.out_path) if clip.out_path else None
            clips.append({**asdict(clip), "posts": db.posts(con, clip.id),
                          "layout": clip.layout or c.render.get("layout", "framed"),
                          "version": int(out.stat().st_mtime) if out and out.exists() else 0})
        waiting = len(db.by_status(con, "discovered"))
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
            creators=[asdict(x) for x in c.creators], clips=clips, waiting=waiting,
            settings=settings, accounts={n: m.connected(c) for n, m in publish.PLATFORMS.items()},
            layouts=render.LAYOUTS, backgrounds=pipeline.backgrounds(c),
            tiktok_redirect=redirect_uri("tiktok"), youtube_redirect=redirect_uri("youtube"),
            hosted=bool(password), job=state["job"], log=list(log)[-60:],
        )

    @app.post("/api/creators")
    def add_creator():
        body = request.get_json()
        if not body.get("permission"):
            return jsonify(error="Tick the box to confirm you're allowed to clip this creator."), 400
        try:
            new = Creator(body.get("platform", ""), body.get("channel", ""), body.get("credit", ""),
                          bool(body.get("all_content")), list(body.get("hashtags") or []))
        except ValueError as e:
            return jsonify(error=str(e)), 400
        c = cfg()
        if any(x.platform == new.platform and x.channel.lower() == new.channel.lower() for x in c.creators):
            return jsonify(error="That streamer is already on your list."), 400
        config.save_settings(c, {"creators": [asdict(x) for x in [*c.creators, new]]})
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
        return jsonify(started=start_job("Finding clips", find_job))

    @app.post("/api/publish")
    def post_all():
        return jsonify(started=start_job("Posting", lambda c, con: pipeline.publish_approved(c, con, log=say)))

    @app.post("/api/retry")
    def retry():
        return jsonify(started=start_job(
            "Retrying", lambda c, con: pipeline.process(c, con, db.by_status(con, "failed"), say)))

    @app.post("/api/clips/<path:clip_id>/layout")
    def set_layout(clip_id):
        """Change a clip's layout / gameplay video / headline and re-edit it."""
        body = request.get_json()
        c = cfg()
        con = db.connect(c.db_path)
        clip = db.get(con, clip_id)
        if not clip:
            abort(404)
        if clip.status not in ("rendered", "approved", "rejected", "failed"):
            return jsonify(error=f"clip is {clip.status} and can't be re-edited"), 400
        layout, background = body.get("layout", ""), body.get("background", "")
        if layout not in render.LAYOUTS:
            return jsonify(error="unknown layout"), 400
        if layout == "split":
            if not pipeline.backgrounds(c):
                return jsonify(error="Add a gameplay video first - the split layout needs one."), 400
            if background and background not in pipeline.backgrounds(c):
                return jsonify(error="That gameplay video no longer exists."), 400
        title = (body.get("title") or clip.title).strip()

        def job(c, con):
            db.update(con, clip_id, layout=layout, background=background, title=title)
            pipeline.process(c, con, [db.get(con, clip_id)], say)

        return jsonify(started=start_job("Re-editing clip", job))

    @app.post("/api/backgrounds")
    def upload_background():
        f = request.files.get("file")
        name = secure_filename(f.filename) if f and f.filename else ""
        if not name or Path(name).suffix.lower() not in render.BACKGROUND_TYPES:
            return jsonify(error="Choose a video file (" + ", ".join(render.BACKGROUND_TYPES) + ")."), 400
        folder = cfg().data_dir / "backgrounds"
        folder.mkdir(parents=True, exist_ok=True)
        f.save(folder / name)
        say(f"Added gameplay video {name}")
        return jsonify(ok=True, name=name)

    @app.post("/api/clips/<path:clip_id>/<action>")
    def set_status(clip_id, action):
        moves = {"approve": (("rendered", "rejected"), "approved"), "reject": (("rendered", "approved"), "rejected")}
        if action not in moves:
            abort(404)
        con = db.connect(cfg().db_path)
        clip = db.get(con, clip_id)
        if not clip:
            abort(404)
        allowed, to = moves[action]
        if clip.status not in allowed:
            return jsonify(error=f"clip is already {clip.status}"), 400
        db.update(con, clip_id, status=to)
        return jsonify(ok=True)

    @app.get("/media/<path:clip_id>")
    def media(clip_id):
        clip = db.get(db.connect(cfg().db_path), clip_id)
        if not clip or not clip.out_path or not Path(clip.out_path).exists():
            abort(404)
        return send_file(Path(clip.out_path).resolve(), mimetype="video/mp4", conditional=True)

    @app.get("/connect/youtube")
    def connect_youtube():
        try:
            url, st, verifier = youtube.authorize_url(cfg(), redirect_uri("youtube"))
        except RuntimeError as e:
            say(str(e))
            return redirect("/#accounts")
        state["youtube_pending"] = (st, verifier)
        return redirect(url)

    @app.get("/oauth/youtube/callback")
    def youtube_callback():
        pending, state["youtube_pending"] = state["youtube_pending"], None
        if not pending or request.args.get("state") != pending[0] or "code" not in request.args:
            say(f"YouTube connect failed: {request.args.get('error', 'unexpected response')}")
        else:
            try:
                youtube.finish_connect(cfg(), request.args["code"], pending[1], redirect_uri("youtube"))
                say("YouTube connected.")
            except Exception as e:
                say(f"YouTube connect failed: {e}")
        return redirect("/#accounts")

    @app.get("/connect/tiktok")
    def connect_tiktok():
        try:
            url, st, verifier = tiktok.authorize_url(cfg(), redirect_uri("tiktok"))
        except RuntimeError as e:
            say(str(e))
            return redirect("/#accounts")
        state["tiktok_pending"] = (st, verifier)
        return redirect(url)

    @app.get("/oauth/tiktok/callback")
    def tiktok_callback():
        pending, state["tiktok_pending"] = state["tiktok_pending"], None
        if not pending or request.args.get("state") != pending[0] or "code" not in request.args:
            say(f"TikTok connect failed: {request.args.get('error_description', 'unexpected response')}")
        else:
            try:
                tiktok.finish_connect(cfg(), request.args["code"], pending[1], redirect_uri("tiktok"))
                say("TikTok connected.")
            except Exception as e:
                say(str(e))
        return redirect("/#accounts")

    @app.post("/api/connect/instagram")
    def connect_instagram():
        try:
            username = instagram.connect(cfg(), request.get_json().get("token", ""))
        except Exception as e:
            return jsonify(error=str(e)), 400
        say(f"Instagram connected{' as @' + username if username else ''}.")
        return jsonify(ok=True)

    @app.post("/api/disconnect/<name>")
    def disconnect(name):
        if name not in publish.PLATFORMS:
            abort(404)
        publish.PLATFORMS[name].disconnect(cfg())
        return jsonify(ok=True)

    app.start_scheduler = lambda: threading.Thread(target=scheduler, daemon=True).start()
    return app


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
