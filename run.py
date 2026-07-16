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


def _interval_minutes(raw: str | None) -> int:
    try:
        return max(1, int(raw or "60"))
    except (TypeError, ValueError):
        return 60


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main() -> int:
    # Create required directories
    for d in ("/config/spotify", "/config/ytmusic", "/config/state", "/music"):
        Path(d).mkdir(parents=True, exist_ok=True)

    # Start gunicorn in background (single worker for OAuth session consistency)
    web = subprocess.Popen(
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

    stop_requested = False
    worker: subprocess.Popen[bytes] | None = None

    def request_stop(*_: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    interval_seconds = _interval_minutes(os.environ.get("MUSIC_SYNC_INTERVAL_MINUTES")) * 60

    try:
        while not stop_requested:
            if web.poll() is not None:
                return web.returncode or 1

            heartbeat()
            worker = subprocess.Popen([sys.executable, "/app/sync.py"])  # noqa: S603
            while worker.poll() is None and not stop_requested:
                if web.poll() is not None:
                    return web.returncode or 1
                time.sleep(1)
            heartbeat()

            for _ in range(interval_seconds):
                if stop_requested:
                    break
                if web.poll() is not None:
                    return web.returncode or 1
                time.sleep(1)
        return 0
    finally:
        if worker is not None:
            _terminate(worker)
        _terminate(web)


if __name__ == "__main__":
    raise SystemExit(main())
