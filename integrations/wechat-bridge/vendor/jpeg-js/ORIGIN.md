# Vendored jpeg-js 0.4.4

These four files (`index.js`, `lib/encoder.js`, `lib/decoder.js`, `LICENSE`) are an
unmodified copy of jpeg-js 0.4.4 (BSD-3-Clause) from the npm tree the bridge image installs
from its committed `package-lock.json`:

```
docker run --rm --network none --entrypoint cat aitelier-wechat-bridge:local \
  /opt/wechat-bridge/node_modules/jpeg-js/package.json | head   # version 0.4.4
```

SHA256 of the copies:

```
404574b5cede8905627bff9b05975c0472d50425feb6906b0a55335e6743c4b4  index.js
a3f175fd6f62d142aad94d3bd90f3a30be4e076baf9b6a6fa31c8e84d9d4aa9f  lib/decoder.js
00bde8df2517eca556669ab6fc27b414e9ba3fc52a0a71e1e37ec7c5f19c3d34  lib/encoder.js
```

`package.json` here only sets `type: commonjs` so Node imports the CJS source from the
bridge's ESM modules; it is not an npm dependency and needs no install. The copy is
committed because the offline test image does not carry the bridge's `node_modules`.
