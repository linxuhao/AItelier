# zvec-grep (zg) sidecar: ripgrep + BM25 + vector search behind one MCP tool.
# The daemon stays on loopback inside this sidecar; a small proxy exposes it
# only to the compose-private network. State and indexes live on the mounted
# ~/.AItelier; each indexed checkout keeps its own .zvec-grep/ directory.
FROM node:22-bookworm-slim
# Resolve Git's common info/exclude path for linked worktrees. The slim base
# image does not promise a Git executable.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g @zvec/zvec-grep@0.2.0 && npm cache clean --force
ENV ZVEC_GREP_HOME=/home/linxuhao/.AItelier/zvec-grep-home \
    HOME=/home/linxuhao/.AItelier/zvec-grep-home
COPY docker/zvec-grep-entrypoint.sh /usr/local/bin/zvec-grep-entrypoint.sh
COPY docker/zvec-grep-proxy.js /usr/local/lib/zvec-grep-proxy.js
RUN chmod +x /usr/local/bin/zvec-grep-entrypoint.sh
CMD ["/usr/local/bin/zvec-grep-entrypoint.sh"]
