#!/usr/bin/env bash
# Build the vLLM argument string for one course candidate and (re)create the `vllm` compose service.
#
# Usage:
#   source m1/env.sh
#   ./m1/serve.sh qwen                              # baseline settings from env.sh
#   ./m1/serve.sh llama --max-model-len 4096        # extra args are appended to vLLM
#   ./m1/serve.sh mistral --kv-cache-dtype fp8 --max-num-seqs 64
#   ./m1/serve.sh qwen --dry-run                    # print VLLM_ARGS and the compose command only
#
# Equivalent by hand:  VLLM_ARGS="--model ..." podman compose up -d --force-recreate vllm
set -euo pipefail

candidate="${1:?usage: serve.sh <qwen|llama|mistral> [extra vllm args...]}"
shift

: "${COMPOSE_FILE:?source m1/env.sh first}"
dry_run=0
extra_args=()
for arg in "$@"; do
  if [[ "$arg" == "--dry-run" ]]; then dry_run=1; else extra_args+=("$arg"); fi
done

case "$candidate" in
  qwen)
    repo="$QWEN_REPO"; revision="$QWEN_REVISION"; served="$QWEN_SERVED_NAME"
    parser_args=(--reasoning-parser "$QWEN_REASONING_PARSER" --enable-auto-tool-choice --tool-call-parser "$QWEN_TOOL_PARSER")
    ;;
  llama)
    repo="$LLAMA_REPO"; revision="$LLAMA_REVISION"; served="$LLAMA_SERVED_NAME"
    parser_args=(--enable-auto-tool-choice --tool-call-parser "$LLAMA_TOOL_PARSER")
    ;;
  mistral)
    repo="$MISTRAL_REPO"; revision="$MISTRAL_REVISION"; served="$MISTRAL_SERVED_NAME"
    # Quantization is read from config.json (auto-round); --config-format mistral would skip it.
    parser_args=(--tokenizer-mode "$MISTRAL_TOKENIZER_MODE" --limit-mm-per-prompt '{"image":0}'
                 --enable-auto-tool-choice --tool-call-parser "$MISTRAL_TOOL_PARSER")
    ;;
  *)
    echo "unknown candidate: $candidate (expected qwen, llama, or mistral)" >&2; exit 2 ;;
esac

vllm_args=(
  --model "$repo"
  --revision "$revision"
  --served-model-name "$served"
  --max-model-len "$MAX_MODEL_LEN"
  --gpu-memory-utilization "$GPU_MEMORY_UTILIZATION"
  --max-num-seqs "$MAX_NUM_SEQS"
  --enable-prefix-caching
  "${parser_args[@]}"
  "${extra_args[@]}"
)
VLLM_ARGS="$(printf '%q ' "${vllm_args[@]}")"
export VLLM_ARGS

echo "vLLM launch arguments for ${candidate} (${repo}@${revision}):"
printf '  %s\n' "$VLLM_ARGS"
echo
# Persist the exact argument string so smoke_test.py --serving-args can record it verbatim.
printf '%s\n' "$VLLM_ARGS" > "${RESULTS_DIR:-.}/last-serving-args.txt"

echo "podman compose up -d --force-recreate vllm    # image: ${VLLM_IMAGE}"
if (( dry_run )); then
  exit 0
fi

podman compose up -d --force-recreate vllm
echo
echo "Started. Follow the load with:  podman compose logs -f vllm"
echo "Ready when you see 'Application startup complete'. Health:  curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:${VLLM_PORT}/health"
