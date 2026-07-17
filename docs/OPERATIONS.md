# Operations and integration contract

This service is intentionally standalone. The parent Homelab Toolkit deploys
the container and owns routing, secrets, metrics scraping, and update policy.

## Stable HTTP contract

| Method | Path | Authentication | Meaning |
| --- | --- | --- | --- |
| GET | `/health` | none | Process health. Returns `200` only when the admin password is configured. |
| GET | `/api/status` | HTTP Basic | JSON status consumed by the service plugin. Existing fields remain stable; `sync_status`, run timestamps, duration, and per-source outcomes are additive. |
| POST | `/api/sync` | HTTP Basic | Starts one asynchronous sync. Returns `202 {"status":"started"}` or `409 {"status":"already_running"}`. |
| GET | `/metrics` | none | Prometheus text exposition. It contains no credentials or provider payloads. |
| GET | `/spotify/start` | HTTP Basic | Starts the Spotify authorization-code flow. |
| GET | `/spotify/callback` | HTTP Basic | Exact callback target used by Spotify. State is single-use and expires with the session. |
| GET | `/` | HTTP Basic | Authenticated setup and operations UI. |

`SPOTIPY_REDIRECT_URI` must exactly match the URI registered in the Spotify
Developer Dashboard and the public routed callback URL. The default is only
for local setup and should not be used in production.

## Persistent volumes

`/config` stores provider credentials, token caches, the download archive, and
atomic run state. `/music` stores the two output libraries. The image runs as
UID/GID `1000:1000`; both mounts must be writable by that identity. The parent
deployment must set ownership or mount options accordingly.

The worker never prunes a provider library after a provider fetch fails. A
successful fetch produces a durable `config/state/run.json` outcome and only
then is the desired set eligible for orphan pruning. Downloads are retried on
the next run; failed runs are visible in `/api/status` and metrics.

## Configuration

Required for a healthy authenticated deployment:

| Variable | Purpose |
| --- | --- |
| `MUSIC_SYNC_WEB_USERNAME` | Basic-auth username |
| `MUSIC_SYNC_WEB_PASSWORD` | Basic-auth password |
| `SPOTIPY_CLIENT_ID` / `SPOTIPY_CLIENT_SECRET` | Spotify application credentials when Spotify sources are enabled |
| `SPOTIPY_REDIRECT_URI` | Exact public Spotify callback URL |

Optional variables include `MUSIC_SYNC_WEB_PUBLIC_URL`,
`MUSIC_SYNC_WEB_SECRET`, `SPOTIFY_CACHE_PATH`, `YTMUSIC_AUTH_FILE`,
`MUSIC_SYNC_INTERVAL_MINUTES`, `MUSIC_SYNC_FAILURE_BACKOFF_SECONDS`,
`MUSIC_SYNC_YTMUSIC_LIMIT`, `MUSIC_SYNC_SPOTIFY_SAVED_TRACKS`,
`MUSIC_SYNC_SPOTIFY_PLAYLISTS`, `MUSIC_SYNC_YTMUSIC_LIKED`, and
`MUSIC_SYNC_YTMUSIC_PLAYLISTS`.

## Release and rollback

Release tags (`v*`) publish immutable digest and release tags for
`linux/amd64` and `linux/arm64` to GHCR, with SBOM, provenance, and an image
attestation. The workflow does not publish a mutable `latest` tag and uses the
full commit SHA for immutable traceability. Deploy a digest-pinned image. Roll
back by changing the parent
deployment to the previous verified digest; `/config` and `/music` are
forward-compatible persistent data and must be retained.
