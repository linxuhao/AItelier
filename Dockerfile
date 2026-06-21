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

FROM python:3.12-slim

# Match the host user so files written into the mounted ~/.AItelier keep host
# ownership, and so HOME points at a writable directory for library caches.
ARG HOME_DIR=/home/app
ARG APP_UID=1000
ARG APP_GID=1000

# git: workspace_manager runs git init/add/commit/clone for every project.
# curl: container healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl \
    && rm -rf /var/lib/apt/lists/*

# Create a writable HOME for the runtime user (HOME/.AItelier is the mount).
RUN mkdir -p "${HOME_DIR}" && chown "${APP_UID}:${APP_GID}" "${HOME_DIR}"
ENV HOME=${HOME_DIR}

WORKDIR /app

# Install the package + dependencies. The source is also bind-mounted at runtime
# (docker-compose) so code edits are live without a rebuild.
COPY . /app
RUN pip install --no-cache-dir -e .

# Identity for in-container git commits (workspace_manager commits set no inline
# identity, and there is no global ~/.gitconfig in the image). Overridable.
ENV GIT_AUTHOR_NAME="AItelier" \
    GIT_AUTHOR_EMAIL="aitelier@localhost" \
    GIT_COMMITTER_NAME="AItelier" \
    GIT_COMMITTER_EMAIL="aitelier@localhost"

EXPOSE 4444

# Bind 0.0.0.0 so the port is reachable from the host / a Cloudflare tunnel.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "4444"]
