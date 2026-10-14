# model-host

The GPU host's deployment unit: the vLLM model server, the nginx gateway in front of it, and the host
and GPU exporters. One Podman Compose project, `taco-model`, managed independently of everything on
the desktop. Used from module 1 onward; exporters from module 4.

```
model-host/
  compose.yaml              project taco-model: vllm (GPU via CDI), gateway (nginx), node-exporter,
                            dcgm-exporter, nvidia-smi-exporter (fallback)
  env.sh                    host settings, image tags, COMPOSE_FILE, the three candidate definitions, and
                            the `tools` shell function (edit before recording)
  serve.sh                  build VLLM_ARGS for a candidate and `podman compose up -d --force-recreate vllm`
  gateway/nginx.conf        reverse proxy on :8080 -> fixed vllm upstream address; keeps the caller's
                            X-Request-ID, JSON access log (module 6 adds TLS and keys)
  exporters/dcgm-counters.csv   the GPU fields a GeForce card reports
  sample_vram.sh            host-side nvidia-smi sampler; feeds the screen tool's --vram-log
```

Evaluation and load clients are not part of this project; they are in [`../tools`](../tools/README.md)
and run on either host. Results go to `../results/` in this checkout.

## One-time setup

Requirements: Podman 4.x+ with `podman compose` (the `podman-compose` package or the docker-compose
provider), the NVIDIA driver, and the NVIDIA Container Toolkit for CDI device injection.

```bash
git clone https://github.com/ned1313/Deploying-and-Operating-Small-Language-Model-Systems && cd Deploying-and-Operating-Small-Language-Model-Systems
chmod +x model-host/serve.sh model-host/sample_vram.sh
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml     # expose the GPU to podman as nvidia.com/gpu=all
source model-host/env.sh                                        # exports COMPOSE_FILE so `podman compose` works anywhere
tools build                                                     # the tools image, for smoke tests on this host
```

Before recording, open [env.sh](env.sh) and:

1. Replace the placeholder Hugging Face repository ids with verified vLLM-loadable quantized
   artifacts, and pin `*_REVISION` to a commit sha.
2. Pin `VLLM_IMAGE` to a specific tag; confirm the `--reasoning-parser` / `--tool-call-parser`
   names exist in that tag (`podman run --rm "$VLLM_IMAGE" --help | grep -A5 parser`).
3. Export `HF_TOKEN` in the shell for gated repositories. Compose passes it to the container by
   name; nothing in this folder writes it to disk.
4. If `10.89.7.0/24` overlaps your LAN, change `INFERENCE_SUBNET` / `VLLM_UPSTREAM_IP` and the
   `server` line in [gateway/nginx.conf](gateway/nginx.conf).

## Commands

Source `env.sh` first in every shell. It exports `COMPOSE_FILE` and `COMPOSE_PROJECT_NAME`, so plain
`podman compose ...` targets this project from any directory.

```bash
./model-host/serve.sh qwen                         # (re)create vllm for a candidate: qwen | llama | granite
podman compose logs -f vllm                        # ready at "Application startup complete"
podman compose up -d gateway
podman compose up -d node-exporter dcgm-exporter   # module 4
```

`tools` (defined by `env.sh`) runs compose commands for the tools project from any directory, with
the variables above cleared: `tools run --rm smoke --skip-structured`. On this host, `tools/.env`
should set `INFERENCE_BASE_URL=http://host.containers.internal:8080/v1` (the default when the file is
absent), which reaches the gateway's published port from a container.

## Services

| Service | Kind | What it does |
| --- | --- | --- |
| `vllm` | long-running | OpenAI-compatible server; arguments from `VLLM_ARGS` (`serve.sh`); published on `127.0.0.1:8000` only; weights in the `hf-cache` volume |
| `gateway` | long-running | nginx on `:8080`, the only LAN path to vLLM; also serves vLLM's `/metrics` |
| `node-exporter` | long-running | host CPU, memory, disk, network on `MODEL_HOST:9100` (host network and PID namespace) |
| `dcgm-exporter` | long-running | GPU utilization, framebuffer, power, temperature on `MODEL_HOST:9400` |
| `nvidia-smi-exporter` | long-running | fallback for `dcgm-exporter` on the same port; start one or the other |

The exporters have no authentication in modules 4-5. They listen on `MODEL_HOST` only; if the host
runs a firewall, allow 9100 and 9400 from the desktop's address only. Module 6 revisits metrics access.

If `dcgm-exporter` exits or the `DCGM_FI_DEV_*` lines are missing (driver or DCGM version mismatch),
use the fallback; the Prometheus recording rules normalize both to the same series:

```bash
podman compose stop dcgm-exporter
podman compose up -d nvidia-smi-exporter
```

## Teardown

```bash
podman compose stop vllm                                       # frees the GPU; weights stay in hf-cache
podman compose stop node-exporter dcgm-exporter nvidia-smi-exporter
```

Stopping desktop projects never affects this host, and stopping this host's services never removes
desktop data.
