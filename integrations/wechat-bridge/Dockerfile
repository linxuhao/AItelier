# aitelier-wechat-bridge:local — the WeChat bridge in its own container (README.md).
#
#   docker build -t aitelier-wechat-bridge:local integrations/wechat-bridge
#
# puppet-neo's native addon needs glibc >= 2.38. node:24-bookworm-slim (the stage
# AItelier's own Dockerfile uses) has 2.36, so only its node binary is taken; the
# image itself is python:3.12-slim (Debian trixie, glibc 2.41).
FROM node:24-bookworm-slim AS node

# Build stage: npm is here only. Dependencies come from the committed package-lock
# (exact pins). --ignore-scripts: puppet-neo has no install scripts (its neo.node is in
# the npm tarball); the scripts it skips are listed in README.md.
FROM python:3.12-slim AS build
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
WORKDIR /opt/wechat-bridge
# --chmod: the image must not depend on the checkout's file modes. A checkout whose files
# are 0600 (this host's) would otherwise be copied in root-owned 0600, and uid 1000 could
# not read main.mjs (EACCES at start). The files are read-only for everyone instead.
COPY --chmod=0444 package.json package-lock.json ./
RUN node /usr/local/lib/node_modules/npm/bin/npm-cli.js ci --omit=dev --ignore-scripts --no-audit --no-fund
# The addon's libraries resolve here, and the newest glibc symbol it asks for.
RUN ldd --version | head -n 1 \
    && ldd node_modules/wechaty-puppet-neo/native/linux-x64/neo.node \
    && ! ldd node_modules/wechaty-puppet-neo/native/linux-x64/neo.node | grep -q 'not found'
COPY --chmod=0444 bridge.mjs main.mjs qr-svg.mjs ./
# puppet-neo declares node >= 26; this is node 24. Load both packages, open (then close)
# a native session the way the puppet does, twice, then build, start and stop a fresh
# Wechaty bot with a fresh puppet twice in this one process, with no network: proves the
# addon loads and runs on this node and this glibc, and that each new bot makes a new
# login attempt (offline each attempt fails: one error per start), which is what POST
# /login does (bridge.mjs) - without logging in to anything.
RUN --network=none node --input-type=module -e " \
      const { WechatyBuilder } = await import('wechaty'); \
      const { PuppetNeo } = await import('wechaty-puppet-neo'); \
      for (let i = 0; i < 2; i++) { const session = new PuppetNeo().createSession(); await session.close(); } \
      for (let i = 1; i <= 2; i++) { \
        const bot = WechatyBuilder.build({ puppet: new PuppetNeo() }); \
        let errors = 0, scans = 0; bot.on('error', () => { errors++; }); bot.on('scan', () => { scans++; }); \
        await bot.start(); \
        for (let t = 0; !errors && t < 100; t++) await new Promise(r => setTimeout(r, 100)); \
        if (!errors || scans) throw new Error('bot ' + i + ': no fresh login attempt'); \
        await bot.stop(); } \
      console.log('neo: two native sessions and two fresh bots started and stopped in one process on node', process.version, 'glibc', process.report.getReport().header.glibcVersionRuntime); \
      process.exit(0);"

FROM python:3.12-slim
COPY --from=node /usr/local/bin/node /usr/local/bin/node
# uid 1000: the host's token file (mode 600, owned by the host user) is mounted read-only.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin bridge
COPY --from=build /opt/wechat-bridge /opt/wechat-bridge
WORKDIR /opt/wechat-bridge
USER bridge
# Inside the container only: listen on its interface; run.sh publishes the port on the
# host's 127.0.0.1 alone.
ENV NODE_ENV=production WECHAT_BRIDGE_BIND=0.0.0.0 WECHAT_BRIDGE_PORT=18731
EXPOSE 18731
ENTRYPOINT ["/usr/local/bin/node", "main.mjs"]
