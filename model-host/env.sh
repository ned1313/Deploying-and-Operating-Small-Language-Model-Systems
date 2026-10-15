# Source this on the GPU host before any model-host command:  source model-host/env.sh
#
# The three candidates below are pinned to Hugging Face commit shas and the vLLM image to a tag (module 5).
# A release manifest (model-host/releases/*.json) overrides these values for `release.sh apply`.
# Check parser names against the running image:  podman run --rm "$VLLM_IMAGE" --help | grep -A3 parser

MODEL_HOST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "${MODEL_HOST_DIR}")"
export MODEL_HOST_DIR REPO_DIR

# ----- podman compose --------------------------------------------------------------------------
# With these exported, `podman compose <cmd>` works from any directory.
export COMPOSE_FILE="${MODEL_HOST_DIR}/compose.yaml"
export COMPOSE_PROJECT_NAME="taco-model"

# ----- host / networking ---------------------------------------------------------------------
export MODEL_HOST="${MODEL_HOST:-$(hostname -I 2>/dev/null | awk '{print $1}')}"   # LAN address the desktop will use
export INFERENCE_SUBNET="${INFERENCE_SUBNET:-10.89.7.0/24}"    # compose network; change if it collides with your LAN
export VLLM_UPSTREAM_IP="${VLLM_UPSTREAM_IP:-10.89.7.10}"      # fixed address nginx proxies to (see gateway/nginx.conf)
export MODEL_CACHE_VOLUME="${MODEL_CACHE_VOLUME:-hf-cache}"    # named volume holding downloaded weights
export VLLM_PORT="${VLLM_PORT:-8000}"                          # bound to 127.0.0.1 only
export GATEWAY_PORT="${GATEWAY_PORT:-8080}"                    # nginx, reachable from the desktop
export RESULTS_ROOT="${RESULTS_ROOT:-${REPO_DIR}/results}"     # one subfolder per kind of result
export RESULTS_DIR="${RESULTS_DIR:-${RESULTS_ROOT}/serving}"   # deployment records, serving args, VRAM logs

# ----- images (fully qualified; podman does not assume docker.io) -------------------------------
# Pinned (module 5). The image digests are recorded in model-host/releases/*.json; release.sh checks them.
export VLLM_IMAGE="${VLLM_IMAGE:-docker.io/vllm/vllm-openai:v0.30.0}"
export NGINX_IMAGE="${NGINX_IMAGE:-docker.io/library/nginx:stable}"

# Every candidate is also served as this alias, so the application's INFERENCE_MODEL never changes
# when a release swaps or rolls back the model.
export SERVED_ALIAS="${SERVED_ALIAS:-taco-slm}"

# ----- shared serving defaults (Demo 1.1 / 1.2 baseline) --------------------------------------
export MAX_MODEL_LEN="${MAX_MODEL_LEN:-8192}"
export GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.90}"
export MAX_NUM_SEQS="${MAX_NUM_SEQS:-32}"

# ----- candidate 1: Qwen3-14B ---------------------------------------------------------------------
# AWQ int4 (group 128); vLLM serves it with awq_marlin on Ampere. ~10 GB of weights.
export QWEN_REPO="${QWEN_REPO:-Qwen/Qwen3-14B-AWQ}"
export QWEN_REVISION="${QWEN_REVISION:-31c69efc29464b6bb0aee1398b5a7b50a99340c3}"
export QWEN_SERVED_NAME="qwen3-14b"
export QWEN_QUANT_LABEL="awq-int4"
export QWEN_REASONING_PARSER="${QWEN_REASONING_PARSER:-qwen3}"
export QWEN_TOOL_PARSER="${QWEN_TOOL_PARSER:-hermes}"
# Qwen thinking mode inflates tokens and latency for a structured task; disable it per request.
export QWEN_EXTRA_BODY='{"chat_template_kwargs": {"enable_thinking": false}}'

# ----- candidate 2: Llama-3.1-8B-Instruct -------------------------------------------------------
export LLAMA_REPO="${LLAMA_REPO:-RedHatAI/Meta-Llama-3.1-8B-Instruct-quantized.w8a8}"
export LLAMA_REVISION="${LLAMA_REVISION:-024e24cbe4153670f747383ea3265d0fb197c727}"
export LLAMA_SERVED_NAME="llama-3.1-8b"
export LLAMA_QUANT_LABEL="gptq-int8"
export LLAMA_TOOL_PARSER="${LLAMA_TOOL_PARSER:-llama3_json}"
export LLAMA_EXTRA_BODY='{}'

# ----- candidate 3: Granite-4.2-8B -----------------------------------------------------------------
# FP8 (compressed-tensors); Ampere runs it weight-only (W8A16) via Marlin. ~9.6 GB of weights.
# Thinking is on by default in the chat template; the MXFP4 build with thinking on failed every scenario.
export GRANITE_REPO="${GRANITE_REPO:-ibm-granite/granite-4.2-8b-fp8}"
export GRANITE_REVISION="${GRANITE_REVISION:-6eb3735036ce2010f4ec8319820172a1c06508d7}"
export GRANITE_SERVED_NAME="granite-4.2-8b"
export GRANITE_QUANT_LABEL="fp8-w8a16-marlin"
# The template emits <tool_call><function=...><parameter=...> XML, which the qwen3_coder parser reads.
export GRANITE_TOOL_PARSER="${GRANITE_TOOL_PARSER:-qwen3_coder}"
export GRANITE_EXTRA_BODY='{"chat_template_kwargs": {"enable_thinking": false}}'

# Hugging Face token for gated repositories (Llama). Set it in your shell, never in this file.
: "${HF_TOKEN:=}"
export HF_TOKEN

# Convenience URLs
export BASE_URL_LOCAL="http://localhost:${GATEWAY_PORT}/v1"     # from the GPU host, via the gateway
export BASE_URL_DIRECT="http://127.0.0.1:${VLLM_PORT}/v1"       # from the GPU host, bypassing the gateway
export BASE_URL_REMOTE="http://${MODEL_HOST}:${GATEWAY_PORT}/v1" # from the desktop

mkdir -p "$RESULTS_DIR"

# Run tools/ compose commands from any directory:  tools build;  tools run --rm smoke --skip-structured
# The COMPOSE_* variables above select this project, so they are cleared for the tools project.
tools() { (cd "${REPO_DIR}/tools" && env -u COMPOSE_FILE -u COMPOSE_PROJECT_NAME podman compose "$@"); }

echo "model-host environment loaded. compose=${COMPOSE_FILE}  gateway=${BASE_URL_REMOTE}  results=${RESULTS_ROOT}"
