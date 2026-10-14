#!/usr/bin/env bash
# Sample GPU memory and utilization once per interval into a CSV that screen_models.py can read
# with --vram-log. Run this on the GPU host (needs nvidia-smi), not inside the tools container.
#
# Usage:
#   ./m1/tools/sample_vram.sh "$RESULTS_DIR/vram-qwen.csv" [interval_seconds] &
#   SAMPLER=$!
#   ...run the evaluation...
#   kill "$SAMPLER"
set -euo pipefail

out="${1:?usage: sample_vram.sh <output.csv> [interval_seconds]}"
interval="${2:-1}"

mkdir -p "$(dirname "$out")"
echo "timestamp,memory_used_mib,memory_total_mib,utilization_gpu_pct" > "$out"
trap 'echo "VRAM sampler stopped; $(($(wc -l < "$out") - 1)) samples in $out"' EXIT
while true; do
  nvidia-smi --id=0 --query-gpu=timestamp,memory.used,memory.total,utilization.gpu \
    --format=csv,noheader,nounits | sed 's/, */,/g' >> "$out"
  sleep "$interval"
done
