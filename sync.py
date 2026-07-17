#!/usr/bin/env python3
import fcntl
import json
import os
import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from spotipy import Spotify  # pyright: ignore[reportMissingImports]
from spotipy.oauth2 import SpotifyOAuth  # pyright: ignore[reportMissingImports]
from ytmusicapi import YTMusic  # pyright: ignore[reportMissingImports]

CONFIG_ROOT = Path("/config")
STATE_ROOT = CONFIG_ROOT / "state"
SYNC_LOCK_FILE = STATE_ROOT / "sync.lock"
SPOTIFY_STATE_FILE = STATE_ROOT / "spotify-downloaded.json"
YTMUSIC_ARCHIVE_FILE = STATE_ROOT / "ytmusic-archive.txt"
MUSIC_ROOT = Path("/music")
SPOTIFY_OUTPUT = MUSIC_ROOT / "Spotify"
YTMUSIC_OUTPUT = MUSIC_ROOT / "YouTube Music"
SPOTIFY_CACHE_PATH = os.environ.get("SPOTIFY_CACHE_PATH", "/config/spotify/spotipy-token.json")
YTMUSIC_AUTH_FILE = os.environ.get("YTMUSIC_AUTH_FILE", "/config/ytmusic/headers_auth.json")
SPOTIFY_REDIRECT_URI = os.environ.get(
    "SPOTIPY_REDIRECT_URI",
    os.environ.get("SPOTIFY_REDIRECT_URI", "http://localhost:8888/callback"),
)
SPOTIPY_CLIENT_ID = os.environ.get("SPOTIPY_CLIENT_ID", "")
SPOTIPY_CLIENT_SECRET = os.environ.get("SPOTIPY_CLIENT_SECRET", "")
YTMUSIC_FETCH_LIMIT = int(os.environ.get("MUSIC_SYNC_YTMUSIC_LIMIT", "5000"))
RUN_STATE_FILE = STATE_ROOT / "run.json"


@dataclass(frozen=True, slots=True)
class SpotifyTrack:
    track_id: str
    title: str
    artists: str


@dataclass(slots=True)
class SourceResult:
    name: str
    configured: bool = False
    success: bool = False
    discovered: int = 0
    downloaded: int = 0
    failed: int = 0
    error: str | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _write_run_state(**values: object) -> None:
    state = {"updated_at": _now(), **values}
    temporary = RUN_STATE_FILE.with_suffix(".tmp")
    RUN_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, RUN_STATE_FILE)


def _ensure_paths() -> None:
    for path in [STATE_ROOT, SPOTIFY_OUTPUT, YTMUSIC_OUTPUT]:
        path.mkdir(parents=True, exist_ok=True)


def _load_json_list(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        values = json.loads(path.read_text())
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            return set()
        return set(values)
    except (json.JSONDecodeError, OSError):
        return set()


def _save_json_list(path: Path, values: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(sorted(values), indent=2) + "\n")
    os.replace(temporary, path)


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _prefixed_audio_files(root: Path) -> dict[str, list[Path]]:
    indexed: dict[str, list[Path]] = {}
    if not root.exists():
        return indexed
    for ext in ("*.mp3", "*.opus", "*.m4a", "*.ogg", "*.webm"):
        for path in root.glob(ext):
            prefix = path.name.split(" - ", 1)[0]
            indexed.setdefault(prefix, []).append(path)
    return indexed


def _delete_paths(paths: list[Path]) -> None:
    for path in paths:
        if path.exists():
            path.unlink()


def _spotify_client() -> Spotify | None:
    if not SPOTIPY_CLIENT_ID or not SPOTIPY_CLIENT_SECRET:
        return None
    auth = SpotifyOAuth(
        client_id=SPOTIPY_CLIENT_ID,
        client_secret=SPOTIPY_CLIENT_SECRET,
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope="user-library-read playlist-read-private playlist-read-collaborative",
        cache_path=SPOTIFY_CACHE_PATH,
        open_browser=False,
    )
    token = auth.get_cached_token()
    if not token:
        print("Spotify token cache missing. Run spotify_auth.py first.")
        return None
    return Spotify(auth_manager=auth)


def _spotify_track(item: dict) -> SpotifyTrack | None:
    track = item.get("track") or {}
    track_id = track.get("id")
    title = track.get("name")
    artists = ", ".join(artist.get("name", "") for artist in track.get("artists", []) if artist.get("name"))
    if not track_id or not title or not artists:
        return None
    return SpotifyTrack(track_id=track_id, title=title, artists=artists)


def _iter_spotify_saved_tracks(sp: Spotify) -> Iterable[SpotifyTrack]:
    offset = 0
    while True:
        batch = sp.current_user_saved_tracks(limit=50, offset=offset)
        items = batch.get("items", [])
        if not items:
            break
        for item in items:
            track = _spotify_track(item)
            if track is not None:
                yield track
        offset += len(items)


def _iter_spotify_playlist_tracks(sp: Spotify, playlist_ref: str) -> Iterable[SpotifyTrack]:
    offset = 0
    while True:
        batch = sp.playlist_items(playlist_ref, limit=100, offset=offset)
        items = batch.get("items", [])
        if not items:
            break
        for item in items:
            track = _spotify_track(item)
            if track is not None:
                yield track
        offset += len(items)


def _download_spotify_track(track: SpotifyTrack) -> bool:
    query = f"ytsearch1:{track.artists} - {track.title} official audio"
    try:
        subprocess.run(
            [
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
                str(SPOTIFY_OUTPUT / f"{track.track_id} - %(title)s.%(ext)s"),
                query,
            ],
            check=True,
            timeout=900,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        print(f"Failed to download Spotify track: {track.artists} - {track.title}")
        return False
    return True


def sync_spotify() -> SourceResult:
    sp = _spotify_client()
    if sp is None:
        configured = bool(SPOTIPY_CLIENT_ID and SPOTIPY_CLIENT_SECRET)
        return SourceResult(
            "spotify",
            configured=configured,
            error="OAuth token is not available" if configured else None,
        )

    result = SourceResult("spotify", configured=True)
    desired: dict[str, SpotifyTrack] = {}

    if os.environ.get("MUSIC_SYNC_SPOTIFY_SAVED_TRACKS", "false").lower() == "true":
        for track in _iter_spotify_saved_tracks(sp):
            desired[track.track_id] = track

    for playlist_ref in _split_csv(os.environ.get("MUSIC_SYNC_SPOTIFY_PLAYLISTS", "")):
        for track in _iter_spotify_playlist_tracks(sp, playlist_ref):
            desired[track.track_id] = track

    current_files = _prefixed_audio_files(SPOTIFY_OUTPUT)
    result.discovered = len(desired)
    for track_id, track in desired.items():
        if track_id not in current_files:
            print(f"Spotify: downloading {track.artists} - {track.title}")
            try:
                if _download_spotify_track(track):
                    result.downloaded += 1
                else:
                    result.failed += 1
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                result.failed += 1

    # Never prune after a partial provider response. A successful empty result
    # is safe; an exception above is not.
    for track_id, paths in current_files.items():
        if track_id not in desired:
            print(f"Spotify: pruning orphaned track {track_id}")
            _delete_paths(paths)

    _save_json_list(SPOTIFY_STATE_FILE, set(desired))
    result.success = result.failed == 0
    if result.failed:
        result.error = f"{result.failed} Spotify download(s) failed"
    return result


def _ytmusic_client() -> YTMusic | None:
    auth_file = Path(YTMUSIC_AUTH_FILE)
    if not auth_file.exists():
        print("YouTube Music auth missing. Run ytmusic_auth.py first.")
        return None
    return YTMusic(str(auth_file))


def _ytmusic_playlist_id(ref: str) -> str:
    if "list=" in ref:
        return ref.split("list=", 1)[1].split("&", 1)[0]
    return ref


def _download_youtube_video(video_id: str) -> bool:
    try:
        subprocess.run(
            [
                "yt-dlp",
                "--download-archive",
                str(YTMUSIC_ARCHIVE_FILE),
                "-f",
                "bestaudio",
                "-x",
                "--audio-format",
                "mp3",
                f"https://www.youtube.com/watch?v={video_id}",
                "-o",
                str(YTMUSIC_OUTPUT / "%(id)s - %(title)s.%(ext)s"),
            ],
            check=True,
            timeout=900,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        print(f"Failed to download youtube track: {video_id}")
        return False
    return True


def sync_ytmusic() -> SourceResult:
    client = _ytmusic_client()
    if client is None:
        return SourceResult("ytmusic", configured=Path(YTMUSIC_AUTH_FILE).exists(), error="auth file is not available")

    result = SourceResult("ytmusic", configured=True)
    desired_ids: set[str] = set()
    current_files = _prefixed_audio_files(YTMUSIC_OUTPUT)

    if os.environ.get("MUSIC_SYNC_YTMUSIC_LIKED", "false").lower() == "true":
        liked = client.get_liked_songs(limit=YTMUSIC_FETCH_LIMIT)
        for entry in liked.get("tracks", liked.get("contents", [])):
            video_id = entry.get("videoId")
            if video_id:
                desired_ids.add(video_id)
                if video_id not in current_files:
                    print(f"YouTube Music: downloading liked track {video_id}")
                    try:
                        if _download_youtube_video(video_id):
                            result.downloaded += 1
                        else:
                            result.failed += 1
                    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                        result.failed += 1

    for playlist_ref in _split_csv(os.environ.get("MUSIC_SYNC_YTMUSIC_PLAYLISTS", "")):
        playlist = client.get_playlist(_ytmusic_playlist_id(playlist_ref), limit=YTMUSIC_FETCH_LIMIT)
        for entry in playlist.get("tracks", []):
            video_id = entry.get("videoId")
            if video_id:
                desired_ids.add(video_id)
                if video_id not in current_files:
                    print(f"YouTube Music: downloading playlist track {video_id}")
                    try:
                        if _download_youtube_video(video_id):
                            result.downloaded += 1
                        else:
                            result.failed += 1
                    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                        result.failed += 1

    result.discovered = len(desired_ids)
    for video_id, paths in current_files.items():
        if video_id not in desired_ids:
            print(f"YouTube Music: pruning orphaned track {video_id}")
            _delete_paths(paths)
    result.success = result.failed == 0
    if result.failed:
        result.error = f"{result.failed} YouTube Music download(s) failed"
    return result


def main() -> int:
    _ensure_paths()
    started = time.monotonic()
    with SYNC_LOCK_FILE.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("A music sync is already running; skipping this trigger.")
            return 0
        _write_run_state(status="running", started_at=_now())
        results: list[SourceResult] = []
        try:
            results.append(sync_spotify())
            results.append(sync_ytmusic())
        except Exception as exc:  # noqa: BLE001 - persist and surface unexpected provider failures
            _write_run_state(
                status="failed",
                finished_at=_now(),
                duration_seconds=time.monotonic() - started,
                error=str(exc),
            )
            print(f"Sync failed unexpectedly: {exc}")
            return 1
    configured = [result for result in results if result.configured]
    failed = [result for result in configured if not result.success]
    payload = {
        "status": "failed" if failed else "success",
        "finished_at": _now(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "sources": [
            {
                "name": result.name,
                "configured": result.configured,
                "success": result.success,
                "discovered": result.discovered,
                "downloaded": result.downloaded,
                "failed": result.failed,
                "error": result.error,
            }
            for result in results
        ],
    }
    _write_run_state(**payload)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
