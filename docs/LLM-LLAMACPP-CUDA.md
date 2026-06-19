# Yorik LLM backend — local Qwen3.5-9B via llama.cpp + MTP speculative decoding

This is the GPU-accelerated LLM setup used by Yorik's maintainer — an
alternative to the basic Ollama recipe in [INSTALL.md](INSTALL.md)
Step 2 for users who want maximum throughput on a single NVIDIA card.
Everything runs on your machine; no API keys, no cloud calls.

**End state:** an OpenAI-compatible HTTP server at
`http://localhost:8080/v1` serving Qwen3.5-9B at roughly 2-3× the
baseline speed thanks to Multi-Token Prediction speculative decoding,
with thinking mode disabled by default for snappier direct answers.

## What you need

| | |
|---|---|
| **GPU** | NVIDIA, 12 GB+ VRAM recommended (8 GB works with lower context) |
| **Driver** | Recent NVIDIA driver (≥ 535) with CUDA support |
| **OS** | Linux (tested on Ubuntu 24.04). Windows works with WSL2 + Docker Desktop |
| **Disk** | ~8 GB free (model is ~6.5 GB) |
| **RAM** | 16 GB+ system RAM |

Verify the GPU is visible to the host:

```bash
nvidia-smi
```

You should see your card and driver version. If not, install/update
the NVIDIA driver before continuing.

## 1. Install Docker + NVIDIA Container Toolkit

Docker engine:

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker
```

NVIDIA Container Toolkit (so containers can talk to the GPU):

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt update && sudo apt install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

Smoke test from inside a container:

```bash
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
```

You should see the same `nvidia-smi` output as before — proving the
GPU is reachable from inside containers.

## 2. Download the model

The model is **Qwen3.5-9B**, Q5_K_M quantization, GGUF format, **with
MTP heads baked in** (needed for the speculative-decoding speedup).
Without MTP heads the `--spec-type draft-mtp` flag below has nothing
to draft from and the launch will fail.

```bash
mkdir -p ~/models && cd ~/models
wget https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/resolve/main/Qwen3.5-9B-Q5_K_M.gguf
```

File should be ~6.5 GB. Verify with `ls -lh Qwen3.5-9B-Q5_K_M.gguf`.

## 3. Create the compose file

```bash
mkdir -p ~/llm-yorik && cd ~/llm-yorik
```

Save as `~/llm-yorik/compose.yaml`:

```yaml
services:
  llama-9b-mtp:
    image: ghcr.io/ggml-org/llama.cpp:server-cuda
    container_name: llama-9b-mtp
    restart: on-failure
    ports:
      - "8080:8080"
    volumes:
      - /home/YOURNAME/models/Qwen3.5-9B-Q5_K_M.gguf:/models/Qwen3.5-9B-Q5_K_M.gguf:ro
    command:
      - "--model"
      - "/models/Qwen3.5-9B-Q5_K_M.gguf"
      - "--alias"
      - "qwen3.5-9b"
      - "--host"
      - "0.0.0.0"
      - "--port"
      - "8080"
      - "--ctx-size"
      - "65536"
      - "--n-gpu-layers"
      - "-1"
      - "--parallel"
      - "1"
      - "--jinja"
      - "-fa"
      - "on"
      - "--cache-type-k"
      - "q4_0"
      - "--cache-type-v"
      - "q4_0"
      - "--spec-type"
      - "draft-mtp"
      - "--spec-draft-n-max"
      - "6"
      - "--temp"
      - "0.6"
      - "--top-p"
      - "0.95"
      - "--top-k"
      - "20"
      - "--min-p"
      - "0.0"
      - "--reasoning"
      - "off"
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
```

Replace `YOURNAME` in the `volumes:` path with your actual Linux
username. Docker Compose does not reliably interpolate `$USER` inside
volume paths — hard-code the absolute path.

## 4. Start it

```bash
cd ~/llm-yorik
docker compose up -d
docker compose logs -f llama-9b-mtp
```

Wait for `HTTP server listening` in the logs. First start takes
30-60 seconds (weight load + warmup). `Ctrl+C` out of the logs
once you see it — the container keeps running.

## 5. Verify

```bash
curl -s http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.5-9b",
    "messages": [{"role": "user", "content": "Say hello in one word."}],
    "max_tokens": 20
  }' | jq
```

Expect `choices[0].message.content` to be populated directly with the
answer. `reasoning_content` should be empty or absent. If you instead
see a long `reasoning_content` and an empty `content`, thinking is
still on — double-check `--reasoning off` made it into the container's
cmdline (`docker inspect llama-9b-mtp --format '{{.Args}}'`).

## 6. Wire it into Yorik

In Yorik's `config.env`:

```
HOMEOS_LLM_BASE_URL=http://localhost:8080/v1
HOMEOS_LLM_MODEL=qwen3.5-9b
```

If Yorik runs inside a VM and the LLM is on the host, swap `localhost`
for the host's address from the VM's perspective (the libvirt default
network gives the host `192.168.122.1`).

Restart Yorik. `/api/health` should report `"llm_reachable": true`.

## What each flag does

| Flag | Why | Tune if… |
|---|---|---|
| `--ctx-size 65536` | 64K context — long agent conversations fit | Drop to `16384`/`32768` if VRAM-tight |
| `--n-gpu-layers -1` | Offload every layer to GPU | Use a specific number to split GPU/CPU when the model doesn't fit |
| `--parallel 1` | One request slot, minimal KV-cache footprint | Bump to `2`-`4` for concurrency; each slot costs ~ctx_size × KV-cache-bytes of VRAM |
| `--jinja` | Use the model's Jinja chat template | Required for per-request thinking control via `chat_template_kwargs` |
| `-fa on` | Flash attention — faster + less VRAM | Leave on |
| `--cache-type-k q4_0` + `--cache-type-v q4_0` | 4-bit KV cache | Saves ~75% of KV-cache VRAM with negligible quality loss at 9B scale |
| `--spec-type draft-mtp` | Multi-Token Prediction speculative decoding | Only works on Qwen3 GGUFs that ship MTP heads. Remove this and `--spec-draft-n-max` if your GGUF doesn't have them |
| `--spec-draft-n-max 6` | Draft up to 6 tokens speculatively per step | `4`-`8` is the sweet spot; higher means more potential speedup but more wasted work on rejection |
| `--temp 0.6 --top-p 0.95 --top-k 20 --min-p 0.0` | Sampling parameters recommended by the Qwen team for Qwen3 | Leave alone unless you know what you're doing |
| `--reasoning off` | Disables Qwen3's thinking mode entirely — model goes straight to the answer | `on` to always think, `auto` to let the chat template decide. Note: `--reasoning-budget N` only **caps** thinking tokens, it does NOT suppress thinking mode — use this `--reasoning` switch instead |

## Enabling thinking when you want it

You don't have to choose at server-start. Two ways to flip it on:

**Permanently** (all requests think): change the compose to
`--reasoning on` (or `auto`), then:

```bash
docker compose up -d --force-recreate llama-9b-mtp
```

**Per request**: with `--jinja` on, the Qwen3 chat template honors:

```json
{
  "model": "qwen3.5-9b",
  "messages": [...],
  "chat_template_kwargs": { "enable_thinking": true }
}
```

Per-request override behavior depends on llama.cpp version when the
server flag is hard `off`. If you want both, set the server to
`--reasoning auto` and let each request opt in or out.

## Operations cheat-sheet

| Action | Command |
|---|---|
| Tail logs | `docker compose logs -f llama-9b-mtp` |
| Restart | `docker compose restart llama-9b-mtp` |
| Update llama.cpp | `docker compose pull && docker compose up -d --force-recreate` |
| Stop | `docker compose down` |
| Stop + remove image | `docker compose down --rmi all` |

## VRAM rough budget at these settings

- Model weights (Q5_K_M, 9B params): ~6.5 GB
- KV cache @ 64K context, q4_0 quantized, parallel=1: ~2 GB
- Activations + flash-attention scratch: ~1-1.5 GB
- **Total: ~10 GB** — fits comfortably on a 12 GB card, tight on
  10 GB, requires layer offloading on 8 GB
