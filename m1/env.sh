# Source this on the GPU host before running the module 1 demos:  source m1/env.sh
#
# VERIFY BEFORE RECORDING: the Hugging Face repository ids, revisions, and vLLM parser names below
# are placeholders for the three course candidates. Confirm each artifact exists, is a vLLM-loadable
# 4-bit/8-bit format (AWQ, GPTQ, FP8, compressed-tensors), and pin its commit sha in *_REVISION.
# Check parser names against the running image:  podman run --rm "$VLLM_IMAGE" --help | grep -A3 parser

M1_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export M1_DIR

# ----- podman compose --------------------------------------------------------------------------
# With these exported, `podman compose <cmd>` works from any directory.
export COMPOSE_FILE="${M1_DIR}/compose.yaml"
export COMPOSE_PROJECT_NAME="taco-m1"

# ----- host / networking ---------------------------------------------------------------------
export MODEL_HOST="${MODEL_HOST:-$(hostname -I 2>/dev/null | awk '{print $1}')}"   # LAN address the desktop will use
export INFERENCE_SUBNET="${INFERENCE_SUBNET:-10.89.7.0/24}"    # compose network; change if it collides with your LAN
export VLLM_UPSTREAM_IP="${VLLM_UPSTREAM_IP:-10.89.7.10}"      # fixed address nginx proxies to (see gateway/nginx.conf)
export MODEL_CACHE_VOLUME="${MODEL_CACHE_VOLUME:-hf-cache}"    # named volume holding downloaded weights
export VLLM_PORT="${VLLM_PORT:-8000}"                          # bound to 127.0.0.1 only
export GATEWAY_PORT="${GATEWAY_PORT:-8080}"                    # nginx, reachable from the desktop
export RESULTS_DIR="${RESULTS_DIR:-${M1_DIR}/results}"

# ----- images (fully qualified; podman does not assume docker.io) -------------------------------
# Pin VLLM_IMAGE to a specific tag before recording and put the same value in the deployment record.
export VLLM_IMAGE="${VLLM_IMAGE:-docker.io/vllm/vllm-openai:latest}"
export NGINX_IMAGE="${NGINX_IMAGE:-docker.io/library/nginx:stable}"

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
export LLAMA_REVISION="${LLAMA_REVISION:-main}"
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
echo "m1 environment loaded. compose=${COMPOSE_FILE}  gateway=${BASE_URL_REMOTE}  results=${RESULTS_DIR}"
