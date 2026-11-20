# MCP SDK bump for the accepted zvec-grep sidecar image, without rebuilding it.
# docker-compose.yml pins aitelier-zg by digest because the accepted image was
# built offline; a fresh docker/zvec-grep.Dockerfile build also resolves
# newer transitive packages (and, as of 2026-11-20, fails: onnxruntime-node
# 1.21.0's install script gets a 404 for its CUDA tarball). This file changes
# only the MCP SDK packages on top of that accepted image:
#   docker tag sha256:4efa1423eb59b0544d8d45828bf93e61189cbbadbe4fdb3ba32d3a2f19837874 aitelier-zg:accepted-4efa1423
#   docker build -f docker/zvec-grep-sdk.Dockerfile \
#     --build-arg BASE=aitelier-zg:accepted-4efa1423 -t aitelier-zg:mcp-sdk-2.3.1 .
# then pin the new image id in docker-compose.yml.
# The RUN body is the same step as in docker/zvec-grep.Dockerfile.
ARG BASE
FROM ${BASE}
ENV HOME=/root
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
ENV HOME=/home/linxuhao/.AItelier/zvec-grep-home
