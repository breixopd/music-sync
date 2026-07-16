#!/bin/sh
set -eu

container_id="$(
  docker run --detach --rm \
    --tmpfs /config \
    --tmpfs /music \
    --env MUSIC_SYNC_WEB_USERNAME=admin \
    --env MUSIC_SYNC_WEB_PASSWORD=smoke-test-password \
    music-sync:test
)"

cleanup() {
  docker rm --force "${container_id}" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

attempt=0
while [ "${attempt}" -lt 30 ]; do
  status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "${container_id}")"
  case "${status}" in
    healthy)
      docker exec "${container_id}" curl -fsS http://localhost:8845/health
      printf '\n'
      exit 0
      ;;
    unhealthy)
      docker logs "${container_id}"
      exit 1
      ;;
  esac
  attempt=$((attempt + 1))
  sleep 2
done

docker logs "${container_id}"
echo "music-sync did not become healthy within 60 seconds" >&2
exit 1
