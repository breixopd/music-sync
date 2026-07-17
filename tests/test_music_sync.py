"""Tests for the music-sync setup UI."""

import base64
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Stub spotipy, ytmusicapi before import
sys.modules["spotipy"] = MagicMock()
sys.modules["spotipy.oauth2"] = MagicMock()
sys.modules["ytmusicapi"] = MagicMock()

APP_PATH = REPO_ROOT / "app.py"
APP_SPEC = importlib.util.spec_from_file_location("homelab_music_sync_app", APP_PATH)
assert APP_SPEC is not None and APP_SPEC.loader is not None
sync_app = importlib.util.module_from_spec(APP_SPEC)
sys.modules[APP_SPEC.name] = sync_app
APP_SPEC.loader.exec_module(sync_app)

SYNC_PATH = REPO_ROOT / "sync.py"
SYNC_SPEC = importlib.util.spec_from_file_location("homelab_music_sync_worker", SYNC_PATH)
assert SYNC_SPEC is not None and SYNC_SPEC.loader is not None
sync_worker = importlib.util.module_from_spec(SYNC_SPEC)
sys.modules[SYNC_SPEC.name] = sync_worker
SYNC_SPEC.loader.exec_module(sync_worker)

AUTH_PATH = REPO_ROOT / "ytmusic_auth.py"
AUTH_SPEC = importlib.util.spec_from_file_location("homelab_ytmusic_auth", AUTH_PATH)
assert AUTH_SPEC is not None and AUTH_SPEC.loader is not None
ytmusic_auth = importlib.util.module_from_spec(AUTH_SPEC)
sys.modules[AUTH_SPEC.name] = ytmusic_auth
AUTH_SPEC.loader.exec_module(ytmusic_auth)


def _auth(username: str = "admin", password: str = "pass") -> dict[str, str]:
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


@pytest.fixture
def client():
    sync_app.app.config["TESTING"] = True
    with sync_app.app.test_client() as c:
        yield c


class TestSyncHealth:
    def test_root_fails_closed_without_configured_password(self, client):
        with patch.object(sync_app, "WEB_PASSWORD", ""):
            resp = client.get("/")

        assert resp.status_code == 503

    def test_health_reports_misconfiguration_without_password(self, client):
        with patch.object(sync_app, "WEB_PASSWORD", ""):
            resp = client.get("/health")

        assert resp.status_code == 503

    def test_root_requires_auth(self, client):
        with patch.object(sync_app, "WEB_PASSWORD", "pass"):
            resp = client.get("/")
            assert resp.status_code == 401

    def test_root_with_auth(self, client):
        with (
            patch.object(sync_app, "WEB_USERNAME", "admin"),
            patch.object(sync_app, "WEB_PASSWORD", "pass"),
        ):
            resp = client.get("/", headers=_auth())
            assert resp.status_code == 200

    def test_authenticated_responses_set_security_headers(self, client):
        with (
            patch.object(sync_app, "WEB_USERNAME", "admin"),
            patch.object(sync_app, "WEB_PASSWORD", "pass"),
        ):
            resp = client.get("/", headers=_auth())

        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert resp.headers["Referrer-Policy"] == "no-referrer"

    def test_status_uses_safe_interval_when_environment_is_invalid(self, client):
        with (
            patch.object(sync_app, "WEB_USERNAME", "admin"),
            patch.object(sync_app, "WEB_PASSWORD", "pass"),
            patch.dict(sync_app.os.environ, {"MUSIC_SYNC_INTERVAL_MINUTES": "invalid"}),
        ):
            resp = client.get("/api/status", headers=_auth())

        assert resp.status_code == 200
        assert resp.json["sync_interval_minutes"] == 60

    def test_metrics_are_scrapable_without_admin_credentials(self, client):
        with patch.object(sync_app, "WEB_PASSWORD", ""):
            response = client.get("/metrics")

        assert response.status_code == 200
        assert b"music_sync_up 1" in response.data
        assert response.mimetype == "text/plain"

    def test_status_includes_persisted_run_state(self, client, tmp_path: Path):
        state_file = tmp_path / "run.json"
        state_file.write_text('{"status": "failed", "error": "provider unavailable"}')
        with (
            patch.object(sync_app, "WEB_USERNAME", "admin"),
            patch.object(sync_app, "WEB_PASSWORD", "pass"),
            patch.object(sync_app, "RUN_STATE_FILE", state_file),
        ):
            response = client.get("/api/status", headers=_auth())

        assert response.json["sync_status"] == "failed"


class TestSpotifyFlow:
    def test_spotify_start_redirects(self, client):
        with (
            patch.object(sync_app, "WEB_USERNAME", "admin"),
            patch.object(sync_app, "WEB_PASSWORD", "pass"),
            patch.object(sync_app, "spotify_oauth") as oauth,
            patch.object(sync_app.secrets, "token_urlsafe", return_value="expected-state"),
        ):
            oauth.return_value.get_authorize_url.return_value = "https://accounts.spotify.test/authorize"
            resp = client.get("/spotify/start", headers=_auth())

        assert resp.status_code == 302
        assert resp.location == "https://accounts.spotify.test/authorize"
        oauth.assert_called_once_with(state="expected-state")

    def test_spotify_callback_rejects_mismatched_state(self, client):
        with (
            patch.object(sync_app, "WEB_USERNAME", "admin"),
            patch.object(sync_app, "WEB_PASSWORD", "pass"),
            client.session_transaction() as session,
        ):
            session["spotify_oauth_state"] = "expected-state"

        with patch.object(sync_app, "WEB_USERNAME", "admin"), patch.object(sync_app, "WEB_PASSWORD", "pass"):
            response = client.get("/spotify/callback?code=code&state=wrong-state", headers=_auth())

        assert response.status_code == 400

    def test_spotify_callback_accepts_matching_state_once(self, client):
        oauth = MagicMock()
        with (
            patch.object(sync_app, "WEB_USERNAME", "admin"),
            patch.object(sync_app, "WEB_PASSWORD", "pass"),
            client.session_transaction() as session,
        ):
            session["spotify_oauth_state"] = "expected-state"

        with (
            patch.object(sync_app, "WEB_USERNAME", "admin"),
            patch.object(sync_app, "WEB_PASSWORD", "pass"),
            patch.object(sync_app, "spotify_oauth", return_value=oauth),
        ):
            response = client.get(
                "/spotify/callback?code=code&state=expected-state",
                headers=_auth(),
            )

        assert response.status_code == 200
        oauth.get_access_token.assert_called_once_with(code="code", as_dict=False)

    def test_spotify_callback_hides_provider_errors(self, client):
        oauth = MagicMock()
        oauth.get_access_token.side_effect = RuntimeError("provider response with secret")
        with (
            patch.object(sync_app, "WEB_USERNAME", "admin"),
            patch.object(sync_app, "WEB_PASSWORD", "pass"),
            client.session_transaction() as session,
        ):
            session["spotify_oauth_state"] = "expected-state"

        with (
            patch.object(sync_app, "WEB_USERNAME", "admin"),
            patch.object(sync_app, "WEB_PASSWORD", "pass"),
            patch.object(sync_app, "spotify_oauth", return_value=oauth),
            patch.object(sync_app.app.logger, "exception") as log_exception,
        ):
            response = client.get(
                "/spotify/callback?code=code&state=expected-state",
                headers=_auth(),
            )

        assert response.status_code == 502
        assert b"secret" not in response.data
        log_exception.assert_called_once()

    def test_spotify_track_download_uses_yt_dlp_without_web_stack(self):
        track = sync_worker.SpotifyTrack(track_id="track-1", title="Example Song", artists="Example Artist")

        with patch.object(sync_worker.subprocess, "run") as run:
            sync_worker._download_spotify_track(track)

        assert run.call_args.args[0] == [
            "yt-dlp",
            "--no-playlist",
            "--extract-audio",
            "--audio-format",
            "mp3",
            "--audio-quality",
            "0",
            "--embed-thumbnail",
            "--embed-metadata",
            "--output",
            str(sync_worker.SPOTIFY_OUTPUT / "track-1 - %(title)s.%(ext)s"),
            "ytsearch1:Example Artist - Example Song official audio",
        ]
        assert run.call_args.kwargs == {"check": True, "timeout": 900}

    def test_spotify_track_requires_identity_and_search_metadata(self):
        assert sync_worker._spotify_track({"track": {"id": "id", "name": "Title", "artists": []}}) is None
        assert sync_worker._spotify_track({"track": None}) is None

        track = sync_worker._spotify_track(
            {"track": {"id": "id", "name": "Title", "artists": [{"name": "Artist"}, {"name": "Guest"}]}}
        )
        assert track == sync_worker.SpotifyTrack(track_id="id", title="Title", artists="Artist, Guest")


class TestSyncCoordination:
    def test_ytmusic_auth_file_is_private(self, tmp_path: Path):
        auth_file = tmp_path / "headers_auth.json"

        def write_auth(*, filepath: str) -> None:
            Path(filepath).write_text("{}")

        with (
            patch.object(ytmusic_auth, "AUTH_FILE", auth_file),
            patch.object(ytmusic_auth, "setup_oauth", side_effect=write_auth),
        ):
            ytmusic_auth.main()

        assert auth_file.stat().st_mode & 0o777 == 0o600

    def test_unconfigured_sources_do_not_delete_existing_files(self):
        with (
            patch.dict(
                sync_worker.os.environ,
                {
                    "MUSIC_SYNC_SPOTIFY_SAVED_TRACKS": "false",
                    "MUSIC_SYNC_SPOTIFY_PLAYLISTS": "",
                },
            ),
            patch.object(sync_worker, "_spotify_client") as client,
        ):
            result = sync_worker.sync_spotify()

        assert result.success is True
        client.assert_not_called()

    def test_bounded_ytmusic_fetch_does_not_prune(self, tmp_path: Path):
        auth_file = tmp_path / "headers_auth.json"
        auth_file.write_text("{}")
        old_file = tmp_path / "old - title.mp3"
        old_file.write_text("audio")
        client = MagicMock()
        client.get_liked_songs.return_value = {"tracks": [{"videoId": "new"}]}
        with (
            patch.object(sync_worker, "YTMUSIC_AUTH_FILE", str(auth_file)),
            patch.object(sync_worker, "YTMUSIC_OUTPUT", tmp_path),
            patch.object(sync_worker, "YTMUSIC_FETCH_LIMIT", 1),
            patch.dict(
                sync_worker.os.environ,
                {"MUSIC_SYNC_YTMUSIC_LIKED": "true", "MUSIC_SYNC_YTMUSIC_PLAYLISTS": ""},
            ),
            patch.object(sync_worker, "_ytmusic_client", return_value=client),
            patch.object(sync_worker, "_download_youtube_video", return_value=True),
            patch.object(sync_worker, "_delete_paths") as delete_paths,
        ):
            result = sync_worker.sync_ytmusic()

        assert result.success is True
        assert result.prune_safe is False
        delete_paths.assert_not_called()

    def test_manual_sync_rejects_duplicate_run(self, client):
        lock = MagicMock()
        lock.acquire.return_value = False
        with (
            patch.object(sync_app, "WEB_USERNAME", "admin"),
            patch.object(sync_app, "WEB_PASSWORD", "pass"),
            patch.object(sync_app, "_sync_lock", lock),
        ):
            response = client.post("/api/sync", headers=_auth())

        assert response.status_code == 409
        assert response.json == {"status": "already_running"}

    def test_state_write_is_atomic(self, tmp_path: Path):
        state_file = tmp_path / "state.json"

        sync_worker._save_json_list(state_file, {"b", "a"})

        assert state_file.read_text() == '[\n  "a",\n  "b"\n]\n'
        assert not state_file.with_suffix(".json.tmp").exists()

    def test_main_skips_when_process_lock_is_held(self, tmp_path: Path):
        lock_file = tmp_path / "sync.lock"
        with (
            patch.object(sync_worker, "SYNC_LOCK_FILE", lock_file),
            patch.object(sync_worker, "_ensure_paths"),
            patch.object(sync_worker, "sync_spotify") as spotify,
            patch.object(sync_worker, "sync_ytmusic") as ytmusic,
            patch.object(sync_worker.fcntl, "flock", side_effect=BlockingIOError),
        ):
            result = sync_worker.main()

        assert result == 0
        spotify.assert_not_called()
        ytmusic.assert_not_called()
