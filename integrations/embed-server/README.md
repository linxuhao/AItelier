# embed-server

llama.cpp's `llama-server` (CPU build) serving dense **bge-m3** embeddings (1024 dims,
L2-normalised) on loopback HTTP, for the steward's optional local vector search
(steward design §3.8.2). The steward's memory (`integrations/dsh-director`,
`lib/memory/vector.mjs`) embeds entries and queries through it and fuses the result with
its built-in BM25 (RRF k=60). It holds no state: it only computes.

It is **off by default**. Nothing in AItelier starts it or talks to it until the owner
enables it (see "Enable in AItelier").

Why a container and not inside the DSH process: onnxruntime-node would add a ~308 MB
native addon to a zero-dependency plugin, a native crash or OOM would take the steward
down with it, and its memory could not be capped on its own. Same pattern as
`integrations/wechat-bridge` (owner, 2026-11-18: build our own image, not a host service).

## Downloads (owner-approved 2026-11-19, done and verified the same day)

Nothing else is downloaded, and nothing is downloaded at run time.

| What | Source | Revision | Bytes | sha256 |
|---|---|---|---|---|
| Model `bge-m3-q8_0.gguf` (MIT) | `huggingface.co/ggml-org/bge-m3-Q8_0-GGUF` | `9eba04c5d75ba5a1595e45de734d36bef4e5cb98` | 634,553,760 | `aa473d51f451a22f0fcf39ba3330c14bed38a385712b1113440f69df4047a173` |
| llama.cpp CPU release `llama-b12119-bin-ubuntu-x64.tar.gz` (MIT) | `github.com/ggml-org/llama.cpp/releases` | b12119, published 2026-11-04T22:34:35Z | 23,078,914 | `b766529b1a5082c65b9dc5a5f1a78092c6e107344dc383c53a00b83da47c16a6` (GitHub release API digest) |
| Base image | Docker Hub `ubuntu:24.04` | `sha256:73b999393d07f2bf65a40373778a44dc9a6d54ad7718d29247d8ff9413c4d6a8` | | |
| `libgomp1` | Ubuntu apt (`noble-updates`), at image build time | 14.2.0-4ubuntu2~24.04.1 | | |

Where they live (neither in the repo nor in `~/.AItelier`):

- model: `~/.local/share/dsh-director/models/bge-m3/bge-m3-q8_0.gguf` (directory 0700,
  file 0400). This directory is mounted into this container only, read-only;
- release tarball: `~/.local/share/aitelier-embed/dl/llama-b12119-bin-ubuntu-x64.tar.gz`;
  `~/.local/share/aitelier-embed/` is the docker build context.

The expected checksums live here, one place each: `model.sha256` (read by `run.sh`)
and `llama.sha256` (read by the image build). Check by hand:

```sh
# from the repository root
(repo=$PWD; cd ~/.local/share/dsh-director/models/bge-m3 && sha256sum -c "$repo/integrations/embed-server/model.sha256")
(repo=$PWD; cd ~/.local/share/aitelier-embed/dl && sha256sum -c "$repo/integrations/embed-server/llama.sha256")
```

## Build

From the repo root (this directory is passed as the named build context
`embed-server`, for `llama.sha256`; the tarball comes from the main context):

```sh
docker build -t aitelier-embed:local \
  -f integrations/embed-server/Dockerfile \
  --build-context embed-server=integrations/embed-server \
  ~/.local/share/aitelier-embed
```

What the build does and checks:

- verifies the tarball against `llama.sha256` (`sha256sum -c`; a mismatch fails the
  build) and unpacks it, with no network;
- installs `libgomp1` (the release is built with OpenMP; the bare image lacks
  `libgomp.so.1`). This is the build's only network use;
- copies the whole release directory as shipped to `/opt/llama` (the CPU backend is
  chosen per microarchitecture at run time from the `libggml-cpu-*.so` beside it);
- runs `ldd` on `llama-server` and every `.so` in the directory and fails on any
  `not found`. Found 2026-11-19: beyond its own libraries it needs `libstdc++`,
  `libgcc_s`, `libc`, `libm`, `libssl.so.3`, `libcrypto.so.3` (all in the base image) and
  `libgomp.so.1`;
- runs as uid 1000 (the base image's own `ubuntu` user; it already has uid 1000, so no
  `useradd`);
- smoke, with no network: `RUN --network=none llama-server --version && llama-server
  --help | grep -q -- --offline` (prints `version: 0.9.0-dev (build 12119, commit
  6f1e65437)`). `RUN --network` needs BuildKit, Docker's default builder; no
  `# syntax=` line, so no frontend image is pulled.

## Run

```sh
integrations/embed-server/run.sh
```

`run.sh` (nothing runs it for you):

1. checks the model file against `model.sha256`; missing or different → exit 78, nothing
   starts;
2. uses its own Docker network `aitelier-embed` (created if missing). That network can
   still reach out; the process stays local because it opens only a local path (`-m`),
   runs with `--offline`, and never uses `-hf`;
3. starts container `aitelier-embed` with `--restart unless-stopped`, `--read-only
   --tmpfs /tmp:size=16m`, `--cap-drop ALL --security-opt no-new-privileges`,
   `--memory 1200m --memory-swap 1200m` (a backstop, not the budget), `--cpu-shares 256
   --pids-limit 64`, `--ulimit core=0`, port `127.0.0.1:${EMBED_PORT:-18751}:8080` only,
   and the model directory read-only at `/models` as its only mount. No `--cpus`: `-t 4`
   already bounds compute, and a CFS quota would let HTTP threads stall the compute
   threads;
4. process: `llama-server -m /models/bge-m3-q8_0.gguf --embedding --pooling cls -c 1024
   -b 1024 -ub 1024 -np 1 -t 4 -tb 4 --threads-http 2 --host 0.0.0.0 --port 8080
   --offline`. bge-m3 is a bidirectional encoder: a whole input must fit one ubatch
   (1024 tokens), hence `-c`/`-b`/`-ub` 1024; the plugin sends at most 1000 code points.
   Output is L2-normalised (`--embd-normalize 2`, the default).

No API key: the service is stateless compute and reachable only on loopback. Its CORS
default is `*`, so a web page open in a browser on this host could also send it
embedding requests (compute only; nothing to read back but vectors of its own text).

A container named `aitelier-embed` already existing makes `docker run` fail; remove it
first (`docker rm -f aitelier-embed`) to start it with a new image.

## Verify

```sh
curl -s http://127.0.0.1:18751/health          # {"status":"ok"}
curl -s http://127.0.0.1:18751/v1/embeddings -H 'content-type: application/json' \
  -d '{"input":["业主说下周三去巴黎看牙医","The owner has a dentist appointment in Paris next Wednesday"]}' \
  | node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{for(const d of JSON.parse(s).data){const v=d.embedding;console.log(v.length,Math.hypot(...v).toFixed(6))}})'
# 1024 1.000000   (twice)
docker exec aitelier-embed cat /proc/net/tcp /proc/net/tcp6
```

In `/proc/net/tcp` expect only: the listener `00000000:1F90` (0.0.0.0:8080, uid 1000),
Docker's embedded DNS listener on `127.0.0.11` (uid 0; every user-defined network has
it, it belongs to dockerd), and inbound connections to `:1F90` from the network's
gateway (the published port). No connection from the container to anywhere else.

## Enable in AItelier

Two systemd drop-ins for the DSH host unit; templates are in `systemd/`:

`~/.config/systemd/user/aitelier-dsh-host.service.d/20-memory-vector.conf`:

```ini
[Service]
Environment=AITELIER_DSH_MEMORY_VECTOR_URL=http://127.0.0.1:18751
Environment=AITELIER_DSH_MEMORY_VECTOR_MODEL=bge-m3-q8_0
```

`~/.config/systemd/user/aitelier-dsh-host.service.d/21-memory-tz.conf` (the owner's
time zone for dated lines, day groups and date words, §3.8.1; independent of vectors).
Unset, the steward uses the host's own time zone (`timedatectl`); set it when the
owner lives in another one, with the owner's IANA zone name:

```ini
[Service]
Environment=AITELIER_DSH_MEMORY_TZ=America/New_York
```

Enabling = start the container (`run.sh`), copy the drop-ins, `systemctl --user
daemon-reload`, then restart the DSH host in an agreed window. The plugin then
backfills existing entries in the background; progress shows in `ranked.vector` / `status.vector`
(`backlog <done>/<total>`, then `ok`). Vectors live beside the memory repo in
`<root>/<userId>.vectors.jsonl` (0600, never in git).

## Disable

Remove `20-memory-vector.conf`, `daemon-reload`, restart the DSH host in an agreed
window; ranking is plain BM25 again and there is no `vector` field. Then stop the
container: `docker rm -f aitelier-embed` (and `docker network rm aitelier-embed`). The
leftover `<userId>.vectors.jsonl` is harmless (purges still filter it) and can be
deleted. Stopping the container while the drop-in is still in place is also safe: the
steward keeps answering with BM25, labels `ranked.source` as `bm25 · 本地索引 · 向量不可用`,
and after 10 minutes tells the owner.

## Later: iGPU (not in this step)

Same GGUF, same HTTP API: switch to llama.cpp's Vulkan build (the image ships Mesa),
add `--device /dev/dri --group-add <render gid>` to the container (no host group change
needed) and `-ngl all`. The 780M is UMA, so weights still use host memory; the gain is
latency only (est. 50–250 ms per query). The vectors file header records the model, not
the device, so existing vectors are kept.
