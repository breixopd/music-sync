#!/usr/bin/env python3
import os
from pathlib import Path

from ytmusicapi import setup_oauth

AUTH_FILE = Path(os.environ.get("YTMUSIC_AUTH_FILE", "/config/ytmusic/headers_auth.json"))


def main() -> None:
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    print("Starting YouTube Music OAuth setup...")
    setup_oauth(filepath=str(AUTH_FILE))
    AUTH_FILE.chmod(0o600)
    print(f"YouTube Music auth written to {AUTH_FILE}")


if __name__ == "__main__":
    main()
