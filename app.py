#!/usr/bin/env python3
import hmac
import json
import os
import secrets
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

from flask import (  # pyright: ignore[reportMissingImports]
    Flask,
    Response,
    redirect,
    render_template_string,
    request,
    session,
    url_for,
)
from spotipy.oauth2 import SpotifyOAuth  # pyright: ignore[reportMissingImports]

WEB_USERNAME = os.environ.get("MUSIC_SYNC_WEB_USERNAME", "music-admin")
WEB_PASSWORD = os.environ.get("MUSIC_SYNC_WEB_PASSWORD", "")
app = Flask(__name__)
# The explicit secret is preferred. Falling back to the already-required admin
# password keeps sessions stable across restarts without adding a new deployment
# variable to the homelab contract. A random fallback is only for the already
# fail-closed, unauthenticated development state.
app.secret_key = os.environ.get("MUSIC_SYNC_WEB_SECRET") or WEB_PASSWORD or secrets.token_hex(32)
app.config.update(
    MAX_CONTENT_LENGTH=64 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("MUSIC_SYNC_WEB_PUBLIC_URL", "").startswith("https://"),
    PERMANENT_SESSION_LIFETIME=600,
)

CONFIG_ROOT = Path("/config")
SPOTIFY_CACHE_PATH = Path(os.environ.get("SPOTIFY_CACHE_PATH", "/config/spotify/spotipy-token.json"))
YTMUSIC_AUTH_FILE = Path(os.environ.get("YTMUSIC_AUTH_FILE", "/config/ytmusic/headers_auth.json"))
SYNC_SCRIPT = Path(__file__).resolve().with_name("sync.py")
HEARTBEAT_FILE = Path("/tmp/music-sync-heartbeat")
RUN_STATE_FILE = CONFIG_ROOT / "state" / "run.json"
PUBLIC_URL = os.environ.get("MUSIC_SYNC_WEB_PUBLIC_URL", "")
AUDIO_EXTENSIONS = ("*.mp3", "*.opus", "*.m4a", "*.ogg", "*.webm")
_sync_lock = threading.Lock()


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _positive_int(value: str | None, default: int) -> int:
    try:
        return max(1, int(value or default))
    except (TypeError, ValueError):
        return default


def _count_audio_files(root: Path) -> int:
    if not root.exists():
        return 0
    total = 0
    for pattern in AUDIO_EXTENSIONS:
        total += sum(1 for _ in root.glob(pattern))
    return total


def _heartbeat_age_seconds(raw: str) -> int | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0, int((datetime.now(UTC) - parsed).total_seconds()))


def _read_run_state() -> dict[str, object]:
    try:
        value = json.loads(RUN_STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _metric(name: str, value: object, help_text: str, metric_type: str = "gauge") -> str:
    escaped = help_text.replace("\\", "\\\\").replace("\n", "\\n")
    return f"# HELP {name} {escaped}\n# TYPE {name} {metric_type}\n{name} {value}\n"


def _requires_auth() -> bool:
    return bool(WEB_PASSWORD)


def _unauthorized() -> Response:
    return Response(
        "Authentication required",
        401,
        {"WWW-Authenticate": 'Basic realm="Music Sync Setup"'},
    )


@app.before_request
def protect_setup_ui():
    # Missing generated credentials are a deployment error, never an instruction
    # to expose the setup and sync APIs without authentication.
    if request.path == "/metrics":
        return None
    if not _requires_auth():
        return Response("Setup UI credentials are not configured", status=503)
    if request.path == "/health":
        return None

    auth = request.authorization
    if (
        not auth
        or not hmac.compare_digest(auth.username, WEB_USERNAME)
        or not hmac.compare_digest(auth.password, WEB_PASSWORD)
    ):
        return _unauthorized()
    return None


@app.after_request
def set_security_headers(response: Response) -> Response:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    )
    return response


def spotify_oauth(*, state: str | None = None) -> SpotifyOAuth:
    return SpotifyOAuth(
        client_id=os.environ.get("SPOTIPY_CLIENT_ID", ""),
        client_secret=os.environ.get("SPOTIPY_CLIENT_SECRET", ""),
        redirect_uri=os.environ.get("SPOTIPY_REDIRECT_URI", "http://localhost:8888/callback"),
        scope="user-library-read playlist-read-private playlist-read-collaborative",
        cache_path=str(SPOTIFY_CACHE_PATH),
        open_browser=False,
        state=state,
    )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/status")
def api_status():
    """Status endpoint consumed by the homelab toolkit MusicSyncClient."""
    heartbeat = HEARTBEAT_FILE.read_text().strip() if HEARTBEAT_FILE.exists() else ""
    interval_minutes = _positive_int(os.environ.get("MUSIC_SYNC_INTERVAL_MINUTES"), 60)
    heartbeat_age = _heartbeat_age_seconds(heartbeat)
    run_state = _read_run_state()
    spotify_ready = SPOTIFY_CACHE_PATH.exists()
    ytmusic_ready = YTMUSIC_AUTH_FILE.exists()
    spotify_playlists = _split_csv(os.environ.get("MUSIC_SYNC_SPOTIFY_PLAYLISTS", ""))
    ytmusic_playlists = _split_csv(os.environ.get("MUSIC_SYNC_YTMUSIC_PLAYLISTS", ""))
    spotify_saved = os.environ.get("MUSIC_SYNC_SPOTIFY_SAVED_TRACKS", "false").lower() == "true"
    ytmusic_liked = os.environ.get("MUSIC_SYNC_YTMUSIC_LIKED", "false").lower() == "true"
    spotify_tracks = _count_audio_files(Path("/music/Spotify"))
    ytmusic_tracks = _count_audio_files(Path("/music/YouTube Music"))
    warnings: list[str] = []

    spotify_sources = len(spotify_playlists) + (1 if spotify_saved else 0)
    ytmusic_sources = len(ytmusic_playlists) + (1 if ytmusic_liked else 0)

    if spotify_sources and not (os.environ.get("SPOTIPY_CLIENT_ID") and os.environ.get("SPOTIPY_CLIENT_SECRET")):
        warnings.append("Spotify app credentials are missing")
    elif spotify_sources and not spotify_ready:
        warnings.append("Spotify OAuth is not completed yet")

    if ytmusic_sources and not ytmusic_ready:
        warnings.append("YouTube Music auth is not completed yet")

    return {
        "running": heartbeat_age is not None and heartbeat_age <= max(interval_minutes * 120, 900),
        "last_sync": heartbeat,
        "heartbeat_age_seconds": heartbeat_age,
        "sync_status": run_state.get("status", "unknown"),
        "sync_started_at": run_state.get("started_at"),
        "sync_finished_at": run_state.get("finished_at"),
        "sync_duration_seconds": run_state.get("duration_seconds"),
        "sync_sources": run_state.get("sources", []),
        "sync_interval_minutes": interval_minutes,
        "spotify_ready": spotify_ready,
        "ytmusic_ready": ytmusic_ready,
        "playlists": len(spotify_playlists) + len(ytmusic_playlists),
        "tracks": spotify_tracks + ytmusic_tracks,
        "spotify_tracks": spotify_tracks,
        "ytmusic_tracks": ytmusic_tracks,
        "spotify_playlist_count": len(spotify_playlists),
        "ytmusic_playlist_count": len(ytmusic_playlists),
        "spotify_saved_enabled": spotify_saved,
        "ytmusic_liked_enabled": ytmusic_liked,
        "warnings": warnings,
    }


@app.get("/metrics")
def metrics():
    """Expose stable, dependency-free Prometheus metrics for homelab scraping."""
    state = _read_run_state()
    status = state.get("status", "unknown")
    success = 1 if status == "success" else 0
    running = 1 if status == "running" else 0
    failed = 1 if status == "failed" else 0
    heartbeat = HEARTBEAT_FILE.read_text().strip() if HEARTBEAT_FILE.exists() else ""
    age = _heartbeat_age_seconds(heartbeat)
    lines = [
        _metric("music_sync_up", 1, "Whether the web process is serving requests."),
        _metric("music_sync_run_running", running, "Whether a synchronization run is active."),
        _metric("music_sync_last_run_success", success, "Whether the latest synchronization run succeeded."),
        _metric("music_sync_last_run_failed", failed, "Whether the latest synchronization run failed."),
        _metric(
            "music_sync_heartbeat_age_seconds",
            age if age is not None else -1,
            "Age of the scheduler heartbeat in seconds.",
        ),
        _metric(
            "music_sync_tracks_total",
            _count_audio_files(Path("/music/Spotify")) + _count_audio_files(Path("/music/YouTube Music")),
            "Total downloaded audio files.",
        ),
    ]
    return Response("".join(lines), mimetype="text/plain; version=0.0.4")


@app.post("/api/sync")
def api_sync():
    """Trigger an immediate sync run asynchronously."""
    if not _sync_lock.acquire(blocking=False):
        if request.form:
            return redirect(url_for("index"))
        return {"status": "already_running"}, 409

    def _run():
        try:
            subprocess.run([sys.executable, str(SYNC_SCRIPT)], check=False)  # noqa: S603
        finally:
            _sync_lock.release()

    threading.Thread(target=_run, name="manual-music-sync", daemon=True).start()
    if request.form:
        return redirect(url_for("index"), code=303)
    return {"status": "started"}, 202


@app.get("/")
def index():
    heartbeat = HEARTBEAT_FILE.read_text().strip() if HEARTBEAT_FILE.exists() else "never"
    run_state = _read_run_state()
    spotify_ready = SPOTIFY_CACHE_PATH.exists()
    ytmusic_ready = YTMUSIC_AUTH_FILE.exists()
    return render_template_string(
        TEMPLATE,
        spotify_ready=spotify_ready,
        ytmusic_ready=ytmusic_ready,
        spotify_redirect=os.environ.get("SPOTIPY_REDIRECT_URI", "http://localhost:8888/callback"),
        public_url=PUBLIC_URL,
        heartbeat=heartbeat,
        sync_status=run_state.get("status", "unknown"),
        sync_error=run_state.get("error"),
        sync_duration=run_state.get("duration_seconds"),
        music_dir="/music",
        config_dir=str(CONFIG_ROOT),
        auth_enabled=_requires_auth(),
        web_username=WEB_USERNAME,
        spotify_saved=os.environ.get("MUSIC_SYNC_SPOTIFY_SAVED_TRACKS", "false"),
        spotify_playlists=os.environ.get("MUSIC_SYNC_SPOTIFY_PLAYLISTS", ""),
        ytmusic_liked=os.environ.get("MUSIC_SYNC_YTMUSIC_LIKED", "false"),
        ytmusic_playlists=os.environ.get("MUSIC_SYNC_YTMUSIC_PLAYLISTS", ""),
    )


@app.get("/spotify/start")
def spotify_start():
    state = secrets.token_urlsafe(32)
    session.permanent = True
    session["spotify_oauth_state"] = state
    auth = spotify_oauth(state=state)
    return redirect(auth.get_authorize_url())


@app.get("/spotify/callback")
def spotify_callback():
    code = request.args.get("code", "")
    actual_state = request.args.get("state", "")
    expected_state = session.pop("spotify_oauth_state", "")
    if not code or not actual_state or not expected_state or not hmac.compare_digest(actual_state, expected_state):
        return Response("Invalid or expired Spotify authorization request", status=400)
    auth = spotify_oauth()
    try:
        auth.get_access_token(code=code, as_dict=False)
    except Exception:  # noqa: BLE001 - provider failures must not leak response details
        app.logger.exception("Spotify OAuth token exchange failed")
        return Response("Spotify authorization failed; retry the connection flow", status=502)
    return render_template_string(
        SUCCESS_TEMPLATE,
        title="Spotify Connected",
        message=f"Spotify token saved to {SPOTIFY_CACHE_PATH}",
        back_url=url_for("index"),
    )


TEMPLATE = """
<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>Music Sync Setup</title>
  <style>
    :root { color-scheme: light; font-family: Inter, ui-sans-serif, system-ui, sans-serif;
            color: #17231f; background: #f3f5f0; }
    body { margin: 0 auto; max-width: 960px; padding: clamp(1.25rem, 4vw, 3rem); line-height: 1.5; }
    header { display: flex; align-items: end; justify-content: space-between; gap: 1rem; margin-bottom: 2rem; }
    h1, h2 { letter-spacing: -0.03em; line-height: 1.1; }
    h1 { margin: 0; font-size: clamp(2rem, 5vw, 3.5rem); }
    h2 { margin-top: 0; font-size: 1.2rem; }
    .eyebrow { margin: 0 0 .5rem; color: #60736a; font-size: .75rem; font-weight: 700;
               letter-spacing: .12em; text-transform: uppercase; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 1rem; }
    .card { border: 1px solid #d4ddd3; border-radius: 10px; padding: 1.25rem; margin-bottom: 1rem; background: #fff; }
    .wide { grid-column: 1 / -1; }
    .value { font-size: 1.05rem; font-weight: 700; }
    .muted { color: #60736a; }
    .ok { color: #156f3d; }
    .warn { color: #8a5d00; }
    code, pre { background: #eef2ec; padding: .15rem .3rem; border-radius: 4px; }
    pre { padding: .8rem; overflow-x: auto; white-space: pre-wrap; }
    a.button, button { display: inline-block; border: 0; background: #234b3b; color: white; padding: .65rem .9rem;
               border-radius: 6px; text-decoration: none; font: inherit; cursor: pointer; }
    a.button.secondary { background: #e8eee7; color: #234b3b; }
    button:focus-visible, a:focus-visible { outline: 3px solid #d58a24; outline-offset: 3px; }
    @media (max-width: 520px) { header { display: block; } header .button { margin-top: 1rem; } }
  </style>
</head>
<body>
  <header><div><p class=\"eyebrow\">Operations console</p><h1>Music Sync</h1>
  <p class=\"muted\">Connect providers, inspect the last run, and keep the local library current.</p></div>
  <form method=\"post\" action=\"{{ url_for('api_sync') }}\">
    <button type=\"submit\">Run sync now</button>
  </form></header>

  <div class=\"grid\"><div class=\"card\">
    <h2>Status</h2>
    <p class=\"value\">Latest run:
      <span class=\"{{ 'ok' if sync_status == 'success' else 'warn' if sync_status == 'failed' else '' }}\">
        {{ sync_status }}
      </span>
    </p>
    {% if sync_error %}<p class=\"warn\">{{ sync_error }}</p>{% endif %}
    <p class=\"muted\">Scheduler heartbeat: <strong>{{ heartbeat }}</strong>
      {% if sync_duration %} · {{ sync_duration }}s{% endif %}</p>
    <p>Spotify token:
      <strong class=\"{{ 'ok' if spotify_ready else 'warn' }}\">
        {{ 'ready' if spotify_ready else 'missing' }}</strong></p>
    <p>YouTube Music auth:
      <strong class=\"{{ 'ok' if ytmusic_ready else 'warn' }}\">
        {{ 'ready' if ytmusic_ready else 'missing' }}</strong></p>
    <p>Library mount: <code>{{ music_dir }}</code></p>
    <p>Config mount: <code>{{ config_dir }}</code></p>
    <p>Setup UI auth: <code>{{ web_username }}</code>
      {% if auth_enabled %} with the configured password{% else %} disabled{% endif %}</p>
    {% if public_url %}<p>Public URL: <code>{{ public_url }}</code></p>{% endif %}
  </div>

  <div class=\"card\">
    <h2>Spotify OAuth</h2>
    <p>Spotify redirect URI configured: <code>{{ spotify_redirect }}</code></p>
    <p><a class=\"button\" href=\"{{ url_for('spotify_start') }}\">Start Spotify OAuth</a></p>
    <p>If Spotify OAuth cannot redirect back here, run this manually instead:</p>
    <pre>docker compose exec music-sync python /app/spotify_auth.py</pre>
  </div>

  <div class=\"card\">
    <h2>YouTube Music OAuth</h2>
    <p>Run the helper once and follow the browser flow:</p>
    <pre>docker compose exec music-sync python /app/ytmusic_auth.py</pre>
  </div>

  <div class=\"card wide\">
    <h2>Current Sync Sources</h2>
    <p>Spotify liked songs: <code>{{ spotify_saved }}</code></p>
    <p>Spotify playlists: <code>{{ spotify_playlists or '(none)' }}</code></p>
    <p>YouTube Music liked songs: <code>{{ ytmusic_liked }}</code></p>
    <p>YouTube Music playlists: <code>{{ ytmusic_playlists or '(none)' }}</code></p>
    <p>Run an immediate sync:</p>
    <pre>docker compose exec music-sync python /app/sync.py</pre>
  </div></div>
</body>
</html>
"""

SUCCESS_TEMPLATE = """
<!doctype html>
<html><head><meta charset=\"utf-8\"><title>{{ title }}</title></head>
<body style=\"font-family: Georgia, serif; max-width: 700px; margin: 2rem auto;\">
  <h1>{{ title }}</h1>
  <p>{{ message }}</p>
  <p><a href=\"{{ back_url }}\">Back to setup</a></p>
</body></html>
"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8845)
