#!/usr/bin/env bash
# Apply, roll back, and inspect model releases on the GPU host (module 5).
#
#   source model-host/env.sh
#   ./model-host/release.sh apply r2-llama-3.1-8b      # or a path to a manifest
#   ./model-host/release.sh rollback                   # re-apply the previous release
#   ./model-host/release.sh status
#
# A release manifest (model-host/releases/<id>.json) pins the model repo and commit, the vLLM image
# and its digests, the serving settings, and the app-side inference settings. `apply` recreates vllm
# with those settings through serve.sh (one GPU: the old model stops before the new one loads, so
# inference is unavailable for the maintenance window), waits for the server, checks the image digest,
# runs the smoke test from a tools container, and records current/previous in
# results/serving/release-state.json. Requires jq.
set -euo pipefail

: "${COMPOSE_FILE:?source model-host/env.sh first}"
command -v jq >/dev/null || { echo "jq is required: sudo apt install jq" >&2; exit 2; }

RELEASES="${MODEL_HOST_DIR}/releases"
STATE="${RESULTS_DIR}/release-state.json"
WAIT_SECONDS="${RELEASE_WAIT_SECONDS:-900}"

manifest_path() {
  case "$1" in
    */*|*.json) echo "$1" ;;
    *) echo "${RELEASES}/$1.json" ;;
  esac
}

state() { [[ -f "$STATE" ]] && jq -r ".${1} // empty" "$STATE" || true; }

wait_ready() {
  local served="$1" deadline=$((SECONDS + WAIT_SECONDS))
  echo "Waiting up to ${WAIT_SECONDS}s for ${served} (follow with: podman compose logs -f vllm)"
  until curl -sf "http://127.0.0.1:${VLLM_PORT}/v1/models" | jq -e --arg m "$served" '.data[] | select(.id == $m)' >/dev/null 2>&1; do
    if [[ "$(podman inspect -f '{{.State.Running}}' vllm 2>/dev/null)" != "true" ]]; then
      echo "vllm stopped while loading; last log lines:" >&2
      podman logs --tail 30 vllm >&2 || true
      return 1
    fi
    if (( SECONDS > deadline )); then
      echo "timed out waiting for ${served}" >&2
      return 1
    fi
    sleep 5
  done
  echo "Serving: $(curl -s "http://127.0.0.1:${VLLM_PORT}/v1/models" | jq -c '[.data[].id]')"
}

check_digest() {
  local file="$1" digests
  digests="$(podman image inspect "$VLLM_IMAGE" --format '{{json .RepoDigests}}' 2>/dev/null || echo '[]')"
  if jq -e --argjson have "$digests" '[.serving.image_digests[] | . as $d | $have | index($d)] | any' "$file" >/dev/null; then
    echo "Image digest matches the manifest."
  else
    echo "WARNING: ${VLLM_IMAGE} digests ${digests} do not match the manifest's serving.image_digests." >&2
  fi
}

smoke() {
  local file="$1" id="$2" served="$3" args
  args="$(cat "${RESULTS_DIR}/last-serving-args.txt")"
  (cd "${REPO_DIR}/tools" && env -u COMPOSE_FILE -u COMPOSE_PROJECT_NAME podman compose run --rm smoke \
    --model "$served" --extra-body "$(jq -c .app.inference_extra_body "$file")" \
    --hf-repo "$(jq -r .model.repo "$file")" --revision "$(jq -r .model.revision "$file")" \
    --quantization "$(jq -r .model.quantization "$file")" --chat-template "tokenizer default" \
    --tool-call-parser "$(jq -r .model.tool_call_parser "$file")" \
    --reasoning-parser "$(jq -r '.model.reasoning_parser // "none"' "$file")" \
    --serving-image "$VLLM_IMAGE" --serving-args "$args" --notes "release ${id}")
}

desktop_note() {
  local file="$1" previous="$2" before after
  after="$(jq -c .app.inference_extra_body "$file")"
  before="$( [[ -n "$previous" && -f "$(manifest_path "$previous")" ]] && jq -c .app.inference_extra_body "$(manifest_path "$previous")" || echo "$after")"
  if [[ "$before" == "$after" ]]; then
    echo "Desktop: nothing to change. The application calls ${SERVED_ALIAS}; it reports the new model within 30 s."
  else
    echo "Desktop: set INFERENCE_EXTRA_BODY='${after}' in app/.env and tools/.env, then"
    echo "  cd app && podman-compose up -d --force-recreate complaint-api"
  fi
}

apply() {
  local file id candidate prefix served previous
  file="$(manifest_path "$1")"
  [[ -f "$file" ]] || { echo "no manifest ${file}" >&2; exit 2; }
  id="$(jq -r .release_id "$file")"
  candidate="$(jq -r .candidate "$file")"
  prefix="$(echo "$candidate" | tr '[:lower:]' '[:upper:]')"
  served="$(jq -r .model.served_name "$file")"
  previous="$(state current)"

  export "${prefix}_REPO=$(jq -r .model.repo "$file")"
  export "${prefix}_REVISION=$(jq -r .model.revision "$file")"
  export "${prefix}_SERVED_NAME=${served}"
  export "${prefix}_TOOL_PARSER=$(jq -r .model.tool_call_parser "$file")"
  [[ "$candidate" != "qwen" ]] || export QWEN_REASONING_PARSER="$(jq -r .model.reasoning_parser "$file")"
  export VLLM_IMAGE="$(jq -r .serving.image "$file")"
  export MAX_MODEL_LEN="$(jq -r .serving.max_model_len "$file")"
  export GPU_MEMORY_UTILIZATION="$(jq -r .serving.gpu_memory_utilization "$file")"
  export MAX_NUM_SEQS="$(jq -r .serving.max_num_seqs "$file")"
  mapfile -t extra < <(jq -r '.serving.extra_args[]?' "$file")

  echo "Applying ${id}: $(jq -r .description "$file")"
  echo "Maintenance window: inference is unavailable until ${served} has loaded. Cases submitted now end"
  echo "failed_retryable; retry them afterwards with 'cli retry <case id>'."
  "${MODEL_HOST_DIR}/serve.sh" "$candidate" "${extra[@]}"
  wait_ready "$served"
  check_digest "$file"

  local smoke_ok=true
  smoke "$file" "$id" "$served" || smoke_ok=false
  local history
  history="$( [[ -f "$STATE" ]] && jq -c '.history // []' "$STATE" || echo '[]')"
  [[ "$previous" == "$id" ]] && previous="$(state previous)"
  jq -n --arg current "$id" --arg previous "$previous" --arg at "$(date -u +%FT%TZ)" --arg smoke "$smoke_ok" \
    --argjson history "$history" \
    '{current: $current, previous: $previous, applied_at: $at, smoke_passed: ($smoke == "true"),
      history: ($history + [{release: $current, applied_at: $at, smoke_passed: ($smoke == "true")}])}' > "$STATE"
  echo "Release state: current ${id}, previous ${previous:-none} (${STATE})"
  desktop_note "$file" "$previous"
  if [[ "$smoke_ok" != true ]]; then
    echo "Smoke test FAILED for ${id}. Roll back with: ./model-host/release.sh rollback" >&2
    exit 1
  fi
}

rollback() {
  local target current
  target="$(state previous)"
  current="$(state current)"
  [[ -n "$target" ]] || { echo "no previous release recorded in ${STATE}" >&2; exit 2; }
  echo "Rolling back from ${current} to ${target}"
  apply "$target"
}

status() {
  if [[ -f "$STATE" ]]; then jq . "$STATE"; else echo "no release applied yet (${STATE})"; fi
  echo "Container image: $(podman inspect -f '{{.ImageName}}' vllm 2>/dev/null || echo 'vllm not running')"
  echo "Served models: $(curl -s "http://127.0.0.1:${VLLM_PORT}/v1/models" | jq -c '[.data[] | {id, root}]' 2>/dev/null || echo 'unreachable')"
}

case "${1:-}" in
  apply) apply "${2:?usage: release.sh apply <release id | manifest path>}" ;;
  rollback) rollback ;;
  status) status ;;
  *) echo "usage: release.sh apply <release id | manifest> | rollback | status" >&2; exit 2 ;;
esac
