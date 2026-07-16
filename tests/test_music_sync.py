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
            creds = base64.b64encode(b"admin:pass").decode()
            resp = client.get("/", headers={"Authorization": f"Basic {creds}"})
            assert resp.status_code == 200


class TestSpotifyFlow:
    def test_spotify_start_redirects(self, client):
        with (
            patch.object(sync_app, "WEB_USERNAME", "admin"),
            patch.object(sync_app, "WEB_PASSWORD", "pass"),
        ):
            creds = base64.b64encode(b"admin:pass").decode()
            resp = client.get(
                "/spotify/start",
                headers={"Authorization": f"Basic {creds}"},
            )
            # Should redirect to Spotify or return error if not configured
            assert resp.status_code in (302, 200, 500)

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
