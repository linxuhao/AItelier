# aitelier-wechat-bridge:local — the WeChat bridge in its own container (README.md).
#
#   docker build -t aitelier-wechat-bridge:local integrations/wechat-bridge
#
# The puppet this image runs is a fork of wechaty-puppet-neo: neo-fork/pin.json names
# the upstream commit and the sha256 of every file the patch touches,
# neo-fork/0001-incoming-message-types.patch is the change, and the native stage below
# rebuilds both from those two files. The fork's source is not committed here.
#
# puppet-neo's native addon needs glibc >= 2.38. node:24-bookworm-slim (the stage
# AItelier's own Dockerfile uses) has 2.36, so only its node binary is taken; the
# image itself is python:3.12-slim (Debian trixie, glibc 2.41).
FROM node:24-bookworm-slim AS node

# Native stage: the fork, built from the pin. Rust 1.93.1 is the pin's
# rust-toolchain.toml channel; node comes from the stage above so the same npm and tsc
# the image uses compile the patched TypeScript.
FROM rust:1.93.1-bookworm AS native
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
# npm run scripts call `npm` by name; the package's own `npm test` needs it on PATH.
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
COPY --chmod=0444 neo-fork/pin.json neo-fork/0001-incoming-message-types.patch neo-fork/0002-saved-login.patch /fork-input/
WORKDIR /fork/repo
# The pin is fetched by commit, never by branch, and what arrived is checked against the
# commit pin.json names before anything is read out of it.
RUN git init -q . \
    && git remote add origin https://github.com/wechaty/puppet-neo \
    && git fetch -q --depth 1 origin ddfa5ecd53ba1303d7e424acb47f86989715af89 \
    && git checkout -q --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = ddfa5ecd53ba1303d7e424acb47f86989715af89
# pin.json is the authority: every file it names must be the pinned blob, the patch must
# be the patch whose sha256 it carries, and the patch must apply to those bytes.
RUN node --input-type=module -e " \
      import { readFileSync } from 'node:fs'; \
      import { createHash } from 'node:crypto'; \
      const sha256 = bytes => createHash('sha256').update(bytes).digest('hex'); \
      const pin = JSON.parse(readFileSync('/fork-input/pin.json', 'utf8')); \
      if (!/^[0-9a-f]{40}$/.test(pin.upstream.commit)) throw new Error('pin.json names no commit'); \
      for (const file of pin.files) { \
        const bytes = readFileSync(file.path); \
        if (bytes.length !== file.size) throw new Error(file.path + ': ' + bytes.length + ' bytes, not the pinned ' + file.size); \
        if (sha256(bytes) !== file.sha256) throw new Error(file.path + ' is not the pinned blob'); } \
      const patch = readFileSync('/fork-input/0001-incoming-message-types.patch'); \
      if (sha256(patch) !== pin.patches[0].sha256) throw new Error('the patch is not the pinned patch'); \
      for (const p of pin.patches) { \
        const patch2 = readFileSync('/fork-input/' + p.path.split('/').pop()); \
        if (sha256(patch2) !== p.sha256) throw new Error(p.path + ' is not the pinned patch'); } \
      console.log('neo-fork: ' + pin.files.length + ' pinned blobs match ' + pin.upstream.commit);" \
    && git apply --check /fork-input/0001-incoming-message-types.patch \
    && git apply /fork-input/0001-incoming-message-types.patch \
    && git apply --check /fork-input/0002-saved-login.patch \
    && git apply /fork-input/0002-saved-login.patch
# The Rust test target is built and run here too: the patch changes this crate's own
# signatures, and a build-only stage cannot notice a test that no longer compiles
# (2026-11-22: four call sites sat at the old arity and nothing ever compiled them).
RUN cargo build --release --locked --manifest-path native/Cargo.toml --target-dir native/target \
    && cargo test --release --locked --lib --manifest-path native/Cargo.toml --target-dir native/target
# dist/ is not committed upstream, so rebuild it from the patched TypeScript. The addon is
# put where the package's own tests look for it, then those tests run against this stage's
# addon: the fork is the thing the image later ships, and it is tested before it is copied.
RUN node /usr/local/lib/node_modules/npm/bin/npm-cli.js ci --no-audit --no-fund \
    && mkdir -p native/linux-x64 \
    && cp native/target/release/libpuppet_neo_native.so native/linux-x64/neo.node \
    && node /usr/local/lib/node_modules/npm/bin/npm-cli.js test \
    && sha256sum native/linux-x64/neo.node

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
# The fork replaces the npm tarball's compiled package: its addon and the dist/ rebuilt
# from its TypeScript. Exactly these two paths, nothing else, and read-only like the rest.
COPY --chmod=0444 --from=native /fork/repo/native/target/release/libpuppet_neo_native.so node_modules/wechaty-puppet-neo/native/linux-x64/neo.node
COPY --from=native /fork/repo/dist/ node_modules/wechaty-puppet-neo/dist/
RUN chmod -R a-w,a+rX node_modules/wechaty-puppet-neo/dist \
    && sha256sum node_modules/wechaty-puppet-neo/native/linux-x64/neo.node \
    && grep -q messageType node_modules/wechaty-puppet-neo/dist/puppet-neo.js
# The addon's libraries resolve here, and the newest glibc symbol it asks for.
RUN ldd --version | head -n 1 \
    && ldd node_modules/wechaty-puppet-neo/native/linux-x64/neo.node \
    && ! ldd node_modules/wechaty-puppet-neo/native/linux-x64/neo.node | grep -q 'not found'
COPY --chmod=0444 bridge.mjs main.mjs qr-svg.mjs prepare-image.mjs ./
# The JPEG codec is pure JS, vendored from the bridge image's own pinned npm tree so the
# offline node suite needs no npm install. Its LICENSE stays with it. Not `--chmod=0444`: that
# mode lands on the directories too, and a directory without x cannot be entered - the build's
# own suite still passed because it runs as root, while the runtime user got EACCES on the codec.
# `a+rX` gives the files 0444 and the directories 0555.
COPY vendor ./vendor
RUN chmod -R a-w,a+rX vendor
# The bridge's own node suite runs HERE and nowhere else: the repository's node gate runs only the
# projects the repo-root node_projects.json names (web, integrations/dsh, integrations/dsh-driver),
# and the bridge is not among them, so a bridge change would otherwise reach an image untested. The
# gate installs a project's own dependencies, which is why the aitelier image's dependency set is not
# the reason. The suite reads run.sh and test-fixtures/login-qr.svg from its own directory (it drives
# run.sh with a stand-in `docker`), so those are inputs too. They are removed in the same layer:
# the runtime stage below ships sources only.
COPY bridge.test.mjs qr-svg.test.mjs run.sh ./
COPY test-fixtures ./test-fixtures
RUN node /usr/local/lib/node_modules/npm/bin/npm-cli.js test \
    && rm -rf bridge.test.mjs qr-svg.test.mjs run.sh test-fixtures
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
