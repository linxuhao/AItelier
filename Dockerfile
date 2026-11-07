# Dockerfile — AItelier web UI + backend server (api.main:app)
#
# The container serves both the REST API and the generated web UI (web/).
# It binds 0.0.0.0 so the port is reachable from the host and from a
# Cloudflare tunnel (cloudflared) running alongside it.
#
# State (DB, workspaces, projects) lives in the host's ~/.AItelier, bind-mounted
# at the SAME absolute path inside the container so that Path.home()/.AItelier
# and every absolute path stored in the DB resolve identically on host and in
# the container. That is why HOME is parameterised to the host home dir.

# Stage 1: Build frontend
FROM node:20-slim AS frontend-build
# test comment
WORKDIR /app/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# Pinned DSH runtime, kept outside the live /app source mount.
FROM node:24-bookworm-slim AS dsh-runtime
WORKDIR /opt/aitelier-dsh
COPY integrations/dsh-driver/package.json integrations/dsh-driver/package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund

# Stage 2: Python runtime
FROM python:3.12-slim

# Match the host user so files written into the mounted ~/.AItelier keep host
# ownership, and so HOME points at a writable directory for library caches.
ARG HOME_DIR=/home/app
ARG APP_UID=1000
ARG APP_GID=1000

# git: workspace_manager runs git init/add/commit/clone for every project.
# curl: container healthcheck.
# nodejs+npm: the run_tests tool (5_test step) gates node projects with
# npm install/build/test — without npm in the backend image the gate is
# silently skipped and frontend breakage passes verification.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Create a writable HOME for the runtime user (HOME/.AItelier is the mount).
RUN mkdir -p "${HOME_DIR}" && chown "${APP_UID}:${APP_GID}" "${HOME_DIR}"
ENV HOME=${HOME_DIR}

WORKDIR /app
COPY --from=dsh-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=dsh-runtime /opt/aitelier-dsh /opt/aitelier-dsh

# Install the package + dependencies. The source is also bind-mounted at runtime
# (docker-compose) so code edits are live without a rebuild.
COPY . /app
# Authored repository gates import only this image-owned client module. Keep
# /app and backend packages off their PYTHONPATH; no new host mount is needed.
# --chmod is not decoration: plain COPY preserves the BUILD HOST's mode, and a
# working tree with umask 077 bakes this 0600 root:root while the backend runs as
# uid 1000 - every authored gate then died on PermissionError before measuring.
COPY --chmod=0644 docker/godot/godot_harness.py /opt/aitelier-gate-imports/docker/godot/godot_harness.py
# ...but --chmod also lands on the directories BuildKit creates for that copy,
# and a 0644 directory cannot be traversed: measured on an image built from
# 82bb9718f7 (/opt/aitelier-gate-imports{,/docker,/docker/godot} came out 644
# root:root, and `ls` as uid 1000 answered Permission denied). The file stays
# 0644; the tree has to be searchable or the client cannot be imported at all.
RUN touch /opt/aitelier-gate-imports/docker/__init__.py \
    /opt/aitelier-gate-imports/docker/godot/__init__.py \
 && chmod 0755 /opt/aitelier-gate-imports /opt/aitelier-gate-imports/docker \
    /opt/aitelier-gate-imports/docker/godot \
 && chmod 0644 /opt/aitelier-gate-imports/docker/__init__.py \
    /opt/aitelier-gate-imports/docker/godot/__init__.py
# Copy the compiled SPA bundle from the frontend-build stage — OUTSIDE /app,
# so the .:/app live-source mount (docker-compose) can't shadow it. api/main.py
# serves it via AITELIER_WEB_DIST; no anonymous volume needed, every image
# rebuild + `up -d` is automatically live.
COPY --from=frontend-build /app/web/dist /srv/web_dist
ENV AITELIER_WEB_DIST=/srv/web_dist
# Install the exact reviewed public engine from PyPI, not a mutable host
# checkout or an old image layer.
# The versions come from requirements.lock, through `-c` (constraints), not
# from PyPI at build time: without it two builds of the SAME pyproject.toml
# installed 11 of 95 distributions at different versions (measured 2026-11-06,
# images e0f264d4cf2e vs 42b962ad54). A constraint file rather than
# `-r requirements.lock` keeps pyproject.toml the single place that decides
# WHICH distributions the project declares; the lock only decides which
# version each declared-and-transitively-needed one resolves to.
RUN pip install --no-cache-dir -c requirements.lock -e .
RUN python -c "from core.output_migration import require_output_engine; require_output_engine()"
# Fail the build if a tool-calling litellm completion cannot run (2026-10-28 litellm 1.106.5 incident).
RUN LITELLM_LOCAL_MODEL_COST_MAP=True python -c 'import litellm; r = litellm.completion(model="anthropic/claude-sonnet-5-5", messages=[{"role": "user", "content": "hi"}], tools=[{"type": "function", "function": {"name": "read", "parameters": {"type": "object", "properties": {}}}}], mock_response="ok"); assert r.choices[0].message.content == "ok"'

# Identity for in-container git commits (workspace_manager commits set no inline
# identity, and there is no global ~/.gitconfig in the image). Overridable.
ENV GIT_AUTHOR_NAME="AItelier" \
    GIT_AUTHOR_EMAIL="aitelier@localhost" \
    GIT_COMMITTER_NAME="AItelier" \
    GIT_COMMITTER_EMAIL="aitelier@localhost"

EXPOSE 4444

# Bind 0.0.0.0 so the port is reachable from the host / a Cloudflare tunnel.
# Long-lived SSE/MCP requests must drain before application lifespan cleanup.
# Allow 5s for requests, then Uvicorn cancels them and runs normal cleanup.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "4444", "--timeout-graceful-shutdown", "5"]
