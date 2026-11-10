"""Local web app: a few buttons on top of the same pipeline the command line uses.
Listens on 127.0.0.1 only - it holds your API keys, so it is not meant to be exposed."""
from __future__ import annotations

import threading
import time
import webbrowser
from collections import deque
from dataclasses import asdict
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, request, send_file

from . import config, db, pipeline, publish
from .config import Creator
from .publish import tiktok, youtube

SECRET_FIELDS = {"twitch": ["client_secret"], "youtube_upload": ["client_secret"], "tiktok": ["client_secret"]}
STATIC = Path(__file__).parent / "static"


def create_app(config_path: str = "config.toml", port: int = 8777) -> Flask:
    app = Flask(__name__, static_folder=None)
    log: deque[str] = deque(maxlen=200)
    busy = threading.Lock()
    state = {"job": "", "last_auto_run": time.time(), "tiktok_pending": None}
    tiktok_redirect = f"http://127.0.0.1:{port}/oauth/tiktok/callback"

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
        # Only this machine's browser, and only our own page, may talk to the app.
        if request.host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            abort(403)
        if request.method != "GET" and not request.is_json:
            abort(415)

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
            clips.append({**asdict(clip), "posts": db.posts(con, clip.id)})
        waiting = len(db.by_status(con, "discovered"))
        settings = {k: getattr(c, k) for k in config.SCALARS}
        settings["keywords"] = c.keywords
        settings["hashtags"] = c.render.get("hashtags", [])
        for section in ("twitch", "youtube_upload", "tiktok"):
            values = dict(getattr(c, section))
            for secret in SECRET_FIELDS[section]:
                values[secret + "_set"] = bool(values.pop(secret, ""))
            settings[section] = values
        return jsonify(
            creators=[asdict(x) for x in c.creators], clips=clips, waiting=waiting,
            settings=settings, accounts={n: m.connected(c) for n, m in publish.PLATFORMS.items()},
            tiktok_redirect=tiktok_redirect, job=state["job"], log=list(log)[-60:],
        )

    @app.post("/api/creators")
    def add_creator():
        body = request.get_json()
        if not body.get("permission"):
            return jsonify(error="Tick the box to confirm you're allowed to clip this creator."), 400
        try:
            new = Creator(body.get("platform", ""), body.get("channel", ""), body.get("credit", ""))
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
            changes["render"] = {"hashtags": [h.strip().lstrip("#") for h in body["hashtags"] if h.strip()]}
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

    @app.post("/api/connect/youtube")
    def connect_youtube():
        def job(c, con):
            say("A browser window is opening - sign in to the YouTube channel you want to post to.")
            youtube.connect(c)
            say("YouTube connected.")
        return jsonify(started=start_job("Connecting YouTube", job))

    @app.get("/connect/tiktok")
    def connect_tiktok():
        try:
            url, st, verifier = tiktok.authorize_url(cfg(), tiktok_redirect)
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
                tiktok.finish_connect(cfg(), request.args["code"], pending[1], tiktok_redirect)
                say("TikTok connected.")
            except Exception as e:
                say(str(e))
        return redirect("/#accounts")

    @app.post("/api/disconnect/<name>")
    def disconnect(name):
        if name not in publish.PLATFORMS:
            abort(404)
        publish.PLATFORMS[name].disconnect(cfg())
        return jsonify(ok=True)

    app.start_scheduler = lambda: threading.Thread(target=scheduler, daemon=True).start()
    return app


def serve(config_path: str = "config.toml", port: int = 8777, open_browser: bool = True) -> None:
    app = create_app(config_path, port)
    app.start_scheduler()
    url = f"http://127.0.0.1:{port}"
    print(f"Clipz is running at {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    app.run(host="127.0.0.1", port=port, threaded=True)
