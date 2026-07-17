# Music Sync

Music Sync imports configured Spotify and YouTube Music libraries into a local
music directory. It provides a small authenticated administration interface,
periodic synchronization, manual synchronization, OAuth setup, health checks,
and machine-readable status for homelab automation.

## Container

Release images are published for `linux/amd64` and `linux/arm64`:

```text
ghcr.io/breixopd/music-sync:v1.1.0
```

Use a release digest in production. The Homelab Toolkit service plugin owns the
deployment configuration, secrets, routing, health checks, metrics, and update
rollout.

Required configuration:

| Variable | Purpose |
| --- | --- |
| `SPOTIPY_CLIENT_ID` | Spotify application client ID |
| `SPOTIPY_CLIENT_SECRET` | Spotify application client secret |
| `SPOTIPY_REDIRECT_URI` | Exact Spotify OAuth callback URL |
| `MUSIC_SYNC_WEB_USERNAME` | Administration username |
| `MUSIC_SYNC_WEB_PASSWORD` | Administration password |

The container expects persistent `/config` and `/music` mounts. Optional source
selection and scheduling use the `MUSIC_SYNC_*` variables declared by the
Homelab Toolkit plugin. The image runs as UID/GID `1000:1000`, so those mounts
must be writable by that identity.

The complete endpoint, persistence, recovery, and parent-deployment contract is
documented in [docs/OPERATIONS.md](docs/OPERATIONS.md).

## Development

```bash
uv sync --all-extras --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest --cov=. --cov-report=term-missing --cov-fail-under=35
uv export --locked --no-dev --no-emit-project --format requirements-txt --output-file requirements-audit.txt
uv run pip-audit --strict --requirement requirements-audit.txt
rm requirements-audit.txt
docker build -t music-sync:test .
scripts/container-smoke.sh
```

The application deliberately reports provider failures instead of pruning on
partial source data. The scheduler keeps serving the UI, records the failed
run in `/config/state/run.json`, and retries after a bounded backoff.

Every release tag builds a multi-architecture image, publishes immutable SHA and
release tags to GHCR, and attaches a GitHub artifact attestation.

## Security

Do not commit OAuth credentials or token caches. Report vulnerabilities using
GitHub's private vulnerability reporting flow described in [SECURITY.md](SECURITY.md).

## License

AGPL-3.0-only. See [LICENSE](LICENSE).
