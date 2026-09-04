// Tiny HTTP forwarder for the zvec-grep sidecar.
// zg's server refuses any non-loopback listen address ([LOOPBACK_REQUIRED]),
// so the daemon stays on 127.0.0.1:7999 and this listens on 0.0.0.0:7998 for
// the compose-private network, rewriting Host so the daemon's loopback-only
// checks keep passing. Streams both ways (SSE responses included).
const http = require("http");
const UP = { host: "127.0.0.1", port: 7999 };
http.createServer((req, res) => {
  const headers = { ...req.headers, host: `${UP.host}:${UP.port}` };
  const up = http.request({ ...UP, path: req.url, method: req.method, headers }, (r) => {
    res.writeHead(r.statusCode, r.headers);
    r.pipe(res);
  });
  up.on("error", () => { if (!res.headersSent) res.writeHead(502); res.end("zvec-grep daemon unreachable"); });
  req.pipe(up);
}).listen(7998, "0.0.0.0", () => console.log("zvec-grep proxy 0.0.0.0:7998 -> 127.0.0.1:7999"));
