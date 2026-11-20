# zvec-grep (zg) sidecar: ripgrep + BM25 + vector search behind one MCP tool.
# The daemon stays on loopback inside this sidecar; a small proxy exposes it
# only to the compose-private network. State and indexes live on the mounted
# ~/.AItelier; each indexed checkout keeps its own .zvec-grep/ directory.
FROM node:22-bookworm-slim
# Resolve Git's common info/exclude path for linked worktrees. The slim base
# image does not promise a Git executable.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git python3 util-linux \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g @zvec/zvec-grep@0.2.2 && npm cache clean --force
# MCP SDK bump (GHSA-f897, -46jr, -jm63, -rvq5, -6prh): zvec-grep 0.2.2 pins the
# SDK v2 packages at exactly 2.0.0, no 0.2.x release moves them, and 0.3.x is a
# native rewrite without dist/client/daemon-client.js (zvec-grep-status.mjs).
# Bump the four pins inside the installed package (server/core/client 2.3.1,
# node adapter 2.1.1, whose peer is server ^2.3.0); npm swaps only those.
# GHSA-46jr's guard, maxToolInputElements, is off by default in 2.3.x: set it
# where zvec-grep builds its McpServer, and fail the build if the anchor moved.
# docker/zvec-grep-sdk.Dockerfile applies this same step on top of the
# accepted, digest-pinned image; keep the two RUN bodies identical.
RUN cd /usr/local/lib/node_modules/@zvec/zvec-grep \
    && npm pkg set "dependencies.@modelcontextprotocol/core=2.3.1" \
        "dependencies.@modelcontextprotocol/client=2.3.1" \
        "dependencies.@modelcontextprotocol/server=2.3.1" \
        "dependencies.@modelcontextprotocol/node=2.1.1" \
    && npm install --omit=dev --no-audit --no-fund \
    && sed -i 's|const server = new McpServer({ name: "zvec-grep", version }, {|&\n        maxToolInputElements: 10_000,|' dist/mcp/tools.js \
    && grep -q 'maxToolInputElements: 10_000' dist/mcp/tools.js \
    && test "$(node -p 'require("./node_modules/@modelcontextprotocol/server/package.json").version')" = 2.3.1 \
    && npm cache clean --force
ENV ZVEC_GREP_HOME=/home/linxuhao/.AItelier/zvec-grep-home \
    HOME=/home/linxuhao/.AItelier/zvec-grep-home
COPY docker/zvec-grep-entrypoint.sh /usr/local/bin/zvec-grep-entrypoint.sh
COPY docker/zvec-grep-proxy.js /usr/local/lib/zvec-grep-proxy.js
COPY docker/zvec-grep-status.mjs /usr/local/lib/zvec-grep-status.mjs
COPY core/semantic_index_control.py /usr/local/lib/semantic_index_control.py
RUN chmod +x /usr/local/bin/zvec-grep-entrypoint.sh
CMD ["/usr/local/bin/zvec-grep-entrypoint.sh"]
