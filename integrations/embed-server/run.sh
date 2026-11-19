#!/bin/sh
# Starts the embedding server container (README.md; design §3.8.2). Nothing runs this
# for you. Build the image first (README.md, "Build").
#
# Checks the model file against model.sha256 (next to this script) before anything
# else: a missing or different file exits 78 and nothing starts. The model directory is
# the only host path the container sees, read-only. Only 127.0.0.1:${EMBED_PORT:-18751}
# on the host reaches it (inside, llama-server listens on 8080). It runs on its own
# Docker network, aitelier-embed (created here if missing). That network can still
# reach out; the process stays local because it opens only a local path (-m), runs with
# --offline, and never uses -hf.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
model_dir="$HOME/.local/share/dsh-director/models/bge-m3"

if ! (cd "$model_dir" 2>/dev/null && sha256sum --check --status "$here/model.sha256"); then
  echo "run.sh: $model_dir/bge-m3-q8_0.gguf is missing or does not match $here/model.sha256; not starting" >&2
  exit 78
fi

network=aitelier-embed
docker network inspect "$network" >/dev/null 2>&1 || docker network create "$network" >/dev/null

exec docker run --detach --name aitelier-embed \
  --network "$network" \
  --restart unless-stopped \
  --read-only --tmpfs /tmp:size=16m \
  --cap-drop ALL --security-opt no-new-privileges \
  --memory 1200m --memory-swap 1200m \
  --cpu-shares 256 --pids-limit 64 \
  --ulimit core=0 \
  --publish "127.0.0.1:${EMBED_PORT:-18751}:8080" \
  --volume "$model_dir:/models:ro" \
  aitelier-embed:local \
  -m /models/bge-m3-q8_0.gguf --embedding --pooling cls \
  -c 1024 -b 1024 -ub 1024 -np 1 -t 4 -tb 4 --threads-http 2 \
  --host 0.0.0.0 --port 8080 --offline
