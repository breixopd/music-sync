#!/usr/bin/env python3
import hmac
import os
from datetime import UTC, datetime
from pathlib import Path

from flask import (  # pyright: ignore[reportMissingImports]
    Flask,
    Response,
    redirect,
    render_template_string,
    request,
    url_for,
)
from spotipy.oauth2 import SpotifyOAuth  # pyright: ignore[reportMissingImports]

app = Flask(__name__)
app.secret_key = os.environ.get("MUSIC_SYNC_WEB_SECRET", os.urandom(32).hex())

CONFIG_ROOT = Path("/config")
SPOTIFY_CACHE_PATH = Path(os.environ.get("SPOTIFY_CACHE_PATH", "/config/spotify/spotipy-token.json"))
YTMUSIC_AUTH_FILE = Path(os.environ.get("YTMUSIC_AUTH_FILE", "/config/ytmusic/headers_auth.json"))
HEARTBEAT_FILE = Path("/tmp/music-sync-heartbeat")
PUBLIC_URL = os.environ.get("MUSIC_SYNC_WEB_PUBLIC_URL", "")
WEB_USERNAME = os.environ.get("MUSIC_SYNC_WEB_USERNAME", "music-admin")
WEB_PASSWORD = os.environ.get("MUSIC_SYNC_WEB_PASSWORD", "")
AUDIO_EXTENSIONS = ("*.mp3", "*.opus", "*.m4a", "*.ogg", "*.webm")


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


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


def spotify_oauth() -> SpotifyOAuth:
    return SpotifyOAuth(
        client_id=os.environ.get("SPOTIPY_CLIENT_ID", ""),
        client_secret=os.environ.get("SPOTIPY_CLIENT_SECRET", ""),
        redirect_uri=os.environ.get("SPOTIPY_REDIRECT_URI", "http://localhost:8888/callback"),
        scope="user-library-read playlist-read-private playlist-read-collaborative",
        cache_path=str(SPOTIFY_CACHE_PATH),
        open_browser=False,
    )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/status")
def api_status():
    """Status endpoint consumed by the homelab toolkit MusicSyncClient."""
    heartbeat = HEARTBEAT_FILE.read_text().strip() if HEARTBEAT_FILE.exists() else ""
    interval_minutes = int(os.environ.get("MUSIC_SYNC_INTERVAL_MINUTES", "60") or "60")
    heartbeat_age = _heartbeat_age_seconds(heartbeat)
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
        "heartbeat_age_seconds": heartbeat_age or 0,
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


@app.post("/api/sync")
def api_sync():
    """Trigger an immediate sync run asynchronously."""
    import subprocess
    import threading

    def _run():
        subprocess.run(["python", "/app/sync.py"], check=False)  # noqa: S603 S607

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "started"}, 202


@app.get("/")
def index():
    heartbeat = HEARTBEAT_FILE.read_text().strip() if HEARTBEAT_FILE.exists() else "never"
    spotify_ready = SPOTIFY_CACHE_PATH.exists()
    ytmusic_ready = YTMUSIC_AUTH_FILE.exists()
    return render_template_string(
        TEMPLATE,
        spotify_ready=spotify_ready,
        ytmusic_ready=ytmusic_ready,
        spotify_redirect=os.environ.get("SPOTIPY_REDIRECT_URI", "http://localhost:8888/callback"),
        public_url=PUBLIC_URL,
        heartbeat=heartbeat,
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
    auth = spotify_oauth()
    return redirect(auth.get_authorize_url())


@app.get("/spotify/callback")
def spotify_callback():
    code = request.args.get("code", "")
    if not code:
        return redirect(url_for("index"))
    auth = spotify_oauth()
    auth.get_access_token(code=code, as_dict=False)
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
  <title>Music Sync Setup</title>
  <style>
    body { font-family: Georgia, serif; margin: 2rem auto; max-width: 900px; line-height: 1.5; color: #1d2a26; }
    .card { border: 1px solid #d7dfd7; border-radius: 12px; padding: 1rem 1.25rem;
            margin-bottom: 1rem; background: #fbfcf9; }
    .ok { color: #156f3d; }
    .warn { color: #8a5d00; }
    code, pre { background: #f2f4ee; padding: 0.15rem 0.3rem; border-radius: 4px; }
    pre { padding: 0.8rem; overflow-x: auto; }
    a.button { display: inline-block; background: #234b3b; color: white; padding: 0.6rem 0.9rem;
               border-radius: 8px; text-decoration: none; }
  </style>
</head>
<body>
  <h1>Music Sync Setup</h1>
  <p>This UI shows status, exact OAuth steps, and the commands you need for Spotify and YouTube Music.</p>

  <div class=\"card\">
    <h2>Status</h2>
    <p>Last sync heartbeat: <strong>{{ heartbeat }}</strong></p>
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

  <div class=\"card\">
    <h2>Current Sync Sources</h2>
    <p>Spotify liked songs: <code>{{ spotify_saved }}</code></p>
    <p>Spotify playlists: <code>{{ spotify_playlists or '(none)' }}</code></p>
    <p>YouTube Music liked songs: <code>{{ ytmusic_liked }}</code></p>
    <p>YouTube Music playlists: <code>{{ ytmusic_playlists or '(none)' }}</code></p>
    <p>Run an immediate sync:</p>
    <pre>docker compose exec music-sync python /app/sync.py</pre>
  </div>
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
