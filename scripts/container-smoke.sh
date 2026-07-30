#!/bin/sh
set -eu

container_id="$(
  docker run --detach --rm \
    --read-only \
    --cap-drop=ALL \
    --security-opt=no-new-privileges:true \
    --tmpfs /config:rw,uid=1000,gid=1000 \
    --tmpfs /music:rw,uid=1000,gid=1000 \
    --tmpfs /tmp:rw,noexec,nosuid,size=16m \
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
      docker exec "${container_id}" curl -fsS http://localhost:8845/health >/dev/null
      if docker exec "${container_id}" curl -sSf http://localhost:8845/api/contract >/dev/null 2>&1; then
        echo "unauthenticated contract request unexpectedly succeeded" >&2
        exit 1
      fi
      if docker exec "${container_id}" curl -sSf http://localhost:8845/api/status >/dev/null 2>&1; then
        echo "unauthenticated status request unexpectedly succeeded" >&2
        exit 1
      fi

      contract="$(
        docker exec "${container_id}" \
          curl -fsS -u admin:smoke-test-password http://localhost:8845/api/contract
      )"
      printf '%s' "${contract}" | docker exec -i "${container_id}" python -c '
import json, sys
doc = json.load(sys.stdin)
assert doc.get("service") == "music-sync"
contract = doc.get("integration_contract", {})
assert contract.get("version") == 1
assert contract.get("compatibility") == "1.x"
required = {"manual-sync", "metrics", "provider-authorization", "status"}
assert required.issubset(set(contract.get("capabilities", [])))
'

      status_doc="$(
        docker exec "${container_id}" \
          curl -fsS -u admin:smoke-test-password http://localhost:8845/api/status
      )"
      printf '%s' "${status_doc}" | docker exec -i "${container_id}" python -c '
import json, sys
doc = json.load(sys.stdin)
for field in ("running", "sync_status", "sync_sources", "spotify_ready", "ytmusic_ready", "playlists", "tracks", "warnings"):
    assert field in doc
assert isinstance(doc["running"], bool)
assert isinstance(doc["sync_sources"], list)
assert isinstance(doc["tracks"], int) and doc["tracks"] >= 0
assert isinstance(doc["warnings"], list)
'

      docker exec "${container_id}" curl -fsS http://localhost:8845/health >/dev/null
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
