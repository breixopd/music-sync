#!/usr/bin/env python3
import os
from pathlib import Path

from spotipy import Spotify
from spotipy.oauth2 import SpotifyOAuth

CACHE_PATH = Path(os.environ.get("SPOTIFY_CACHE_PATH", "/config/spotify/spotipy-token.json"))
CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)

client_id = os.environ.get("SPOTIPY_CLIENT_ID", "")
client_secret = os.environ.get("SPOTIPY_CLIENT_SECRET", "")
redirect_uri = os.environ.get("SPOTIPY_REDIRECT_URI", "http://localhost:8888/callback")
scope = "user-library-read playlist-read-private playlist-read-collaborative"

if not client_id or not client_secret:
    raise SystemExit("SPOTIPY_CLIENT_ID and SPOTIPY_CLIENT_SECRET are required")

auth = SpotifyOAuth(
    client_id=client_id,
    client_secret=client_secret,
    redirect_uri=redirect_uri,
    scope=scope,
    cache_path=str(CACHE_PATH),
    open_browser=False,
)

url = auth.get_authorize_url()
print("Open this URL in your browser and authorize access:\n")
print(url)
print("\nPaste the full redirected URL here:")
response_url = input().strip()
code = auth.parse_response_code(response_url)
if not code:
    raise SystemExit("Could not parse authorization code from the callback URL")

auth.get_access_token(code=code, as_dict=False)
spotify = Spotify(auth_manager=auth)
profile = spotify.current_user()
print(f"Spotify OAuth complete for {profile.get('display_name') or profile.get('id')}")
print(f"Token cache written to {CACHE_PATH}")
