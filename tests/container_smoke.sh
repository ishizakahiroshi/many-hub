#!/usr/bin/env bash
# CI-only local container smoke; no image push, remote service, or credentials.
set -euo pipefail
image="many-hub:ci"
volume="many-hub-test-${RANDOM}-${RANDOM}"
workdir="$(mktemp -d)"
worker=""
cleanup() {
  if [[ -n "$worker" ]]; then docker rm -f "$worker" >/dev/null 2>&1 || true; fi
  docker volume rm "$volume" >/dev/null 2>&1 || true
  rm -rf "$workdir"
}
trap cleanup EXIT
chmod 755 "$workdir"
printf '%s\n' '{"request_id":"container-smoke","input":{"text":"durable container mock"}}' > "$workdir/request.json"
chmod 644 "$workdir/request.json"
docker build --tag "$image" .
docker volume create "$volume" >/dev/null
isolation=(--read-only --network none --cap-drop ALL --security-opt no-new-privileges --pids-limit 64 --memory 256m --cpus 1 --init --tmpfs /tmp:rw,noexec,nosuid,size=32m --volume "$volume:/data")
common=(--rm "${isolation[@]}")
docker run "${common[@]}" --entrypoint python "$image" -c 'import os; assert os.getuid() == 10001'
docker run "${common[@]}" --mount "type=bind,source=$workdir/request.json,target=/request.json,readonly" "$image" task create --input-file /request.json --json > "$workdir/created.json"
task_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["task_id"])' "$workdir/created.json")"
docker run "${common[@]}" "$image" worker --once --json > "$workdir/worked.json"
docker run "${common[@]}" "$image" task get "$task_id" --json > "$workdir/result.json"
python3 -c 'import json,sys; t=json.load(open(sys.argv[1])); assert t["state"]=="succeeded"; assert t["result"]["echo"]["text"]=="durable container mock"' "$workdir/result.json"
# Prove the default serve CMD actually stays running and processes new work.
printf '%s\n' '{"request_id":"container-background","input":{"text":"default serve"}}' > "$workdir/request.json"
docker run "${common[@]}" --mount "type=bind,source=$workdir/request.json,target=/request.json,readonly" "$image" task create --input-file /request.json --json > "$workdir/background-created.json"
background_id="$(python3 -c 'import json,sys; t=json.load(open(sys.argv[1])); assert t["state"]=="queued"; print(t["task_id"])' "$workdir/background-created.json")"
worker="$(docker run --detach "${isolation[@]}" "$image")"
for attempt in $(seq 1 50); do
  [[ "$(docker inspect --format '{{.State.Running}}' "$worker")" == "true" ]]
  docker run "${common[@]}" "$image" task get "$background_id" --json > "$workdir/background-result.json"
  if python3 -c 'import json,sys; raise SystemExit(0 if json.load(open(sys.argv[1]))["state"]=="succeeded" else 1)' "$workdir/background-result.json"; then
    break
  fi
  sleep 0.1
done
python3 -c 'import json,sys; t=json.load(open(sys.argv[1])); assert t["state"]=="succeeded"; assert t["result"]["echo"]["text"]=="default serve"' "$workdir/background-result.json"
[[ "$(docker inspect --format '{{.State.Running}}' "$worker")" == "true" ]]
docker stop "$worker" >/dev/null
docker rm "$worker" >/dev/null
worker="$(docker run --detach "${isolation[@]}" "$image")"
sleep 0.2
[[ "$(docker inspect --format '{{.State.Running}}' "$worker")" == "true" ]]
docker run "${common[@]}" "$image" task get "$background_id" --json > "$workdir/restarted.json"
python3 -c 'import json,sys; before=json.load(open(sys.argv[1])); after=json.load(open(sys.argv[2])); assert after["state"]=="succeeded"; assert before["revision"]==after["revision"]; assert before["result"]==after["result"]' "$workdir/background-result.json" "$workdir/restarted.json"
printf '%s\n' 'Container build, non-root confined CLI/default-worker roundtrips, and persistent-volume restart passed.'
