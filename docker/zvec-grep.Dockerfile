# zvec-grep (zg) sidecar: ripgrep + BM25 + vector search behind one MCP tool.
# Shares the aitelier container's network namespace (compose: network_mode),
# because zg's server only listens on loopback — so inside `aitelier`,
# http://127.0.0.1:7999/mcp is this daemon. State and indexes live on the
# mounted ~/.AItelier (ZVEC_GREP_HOME) so they survive recreation; each
# indexed repo also keeps its own .zvec-grep/ under the repo root.
FROM node:22-bookworm-slim
RUN npm install -g @zvec/zvec-grep@0.2.0 && npm cache clean --force
ENV ZVEC_GREP_HOME=/home/linxuhao/.AItelier/zvec-grep-home \
    HOME=/home/linxuhao/.AItelier/zvec-grep-home
COPY docker/zvec-grep-entrypoint.sh /usr/local/bin/zvec-grep-entrypoint.sh
RUN chmod +x /usr/local/bin/zvec-grep-entrypoint.sh
CMD ["/usr/local/bin/zvec-grep-entrypoint.sh"]
