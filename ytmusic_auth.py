#!/usr/bin/env python3
import os
from pathlib import Path

from ytmusicapi import setup_oauth

AUTH_FILE = Path(os.environ.get("YTMUSIC_AUTH_FILE", "/config/ytmusic/headers_auth.json"))
AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)

print("Starting YouTube Music OAuth setup...")
setup_oauth(filepath=str(AUTH_FILE))
print(f"YouTube Music auth written to {AUTH_FILE}")
