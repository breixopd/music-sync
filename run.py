#!/usr/bin/env python3
"""Entrypoint for the music-sync container.

Creates config directories, starts gunicorn (single-worker WSGI for
consistent OAuth sessions), then loops sync + heartbeat at a configurable
interval.
"""

import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path


def heartbeat() -> None:
    Path("/tmp/music-sync-heartbeat").write_text(datetime.now(UTC).isoformat())


def main() -> int:
    # Create required directories
    for d in ("/config/spotify", "/config/ytmusic", "/config/state", "/music"):
        Path(d).mkdir(parents=True, exist_ok=True)

    # Start gunicorn in background (single worker for OAuth session consistency)
    gunicorn = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "gunicorn",
            "--bind",
            "0.0.0.0:8845",
            "--workers",
            "1",
            "--threads",
            "4",
            "--timeout",
            "120",
            "app:app",
        ],
    )

    def cleanup():
        gunicorn.terminate()
        gunicorn.wait(timeout=10)

    signal.signal(signal.SIGTERM, lambda *_: cleanup() or sys.exit(0))
    signal.signal(signal.SIGINT, lambda *_: cleanup() or sys.exit(0))

    # Parse interval from env
    interval_str = os.environ.get("MUSIC_SYNC_INTERVAL_MINUTES", "60")
    try:
        interval_minutes = max(1, int(interval_str))
    except (ValueError, TypeError):
        interval_minutes = 60

    while True:
        heartbeat()
        subprocess.run([sys.executable, "/app/sync.py"], check=False)
        heartbeat()
        time.sleep(interval_minutes * 60)


if __name__ == "__main__":
    raise SystemExit(main())
