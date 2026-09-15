// Search-only HTTP/SSE proxy. Each request acquires durable ownership before
// forwarding; disconnect/error retains unknown work until explicit recovery.
const http = require("http");
const { spawn } = require("child_process");
const UP = { host: "127.0.0.1", port: 7999 };
function createProxy(upstream = UP) {
  return http.createServer((req, res) => {
    // Fixed empty protocol probe: never forwards caller data or invokes tools.
    // Health must remain available while deployment holds operation admission.
    if (req.url === "/health" && req.method === "GET") {
      req.resume();
      const probe = http.request({ ...upstream, path: "/mcp", method: "POST",
        headers: { host: `${upstream.host}:${upstream.port}`, "content-length": "0" } }, (r) => {
        r.resume();
        r.on("end", () => { res.writeHead(r.statusCode < 500 ? 200 : 503); res.end(); });
      });
      probe.on("error", () => { res.writeHead(503); res.end(); });
      probe.end();
      return;
    }
    req.pause();
    const owner = spawn("python3", ["-m", "core.resource_ownership", "hold", "semantic-request"],
                        { stdio: ["pipe", "pipe", "pipe"] });
    let admitted = false, settled = false, up;
    const fail = () => {
      owner.stdin.end(); // EOF is not a settlement acknowledgement.
      if (up) up.destroy();
      if (!res.headersSent) res.writeHead(503);
      res.end("semantic ownership or upstream unavailable");
    };
    owner.on("error", fail);
    owner.stdin.on("error", () => {});
    owner.stderr.on("data", () => {});
    owner.on("exit", (code) => { if (code !== 0 || !admitted) fail(); });
    let ready = "";
    owner.stdout.on("data", (data) => {
      ready += data.toString();
      if (admitted || !ready.includes("\n")) return;
      if (ready !== "admitted\n") return fail();
      admitted = true;
      if (req.destroyed) return fail();
      const headers = { ...req.headers, host: `${upstream.host}:${upstream.port}` };
      up = http.request({ ...upstream, path: req.url, method: req.method, headers }, (response) => {
        res.writeHead(response.statusCode, response.headers);
        response.on("error", fail);
        response.on("end", () => {
          if (response.complete) {
            settled = true;
            owner.stdin.end("settled\n");
          } else fail();
        });
        response.pipe(res);
      });
      up.on("error", fail);
      req.pipe(up);
      req.resume();
    });
    req.on("aborted", fail);
    res.on("close", () => { if (!settled) fail(); });
  });
}
if (require.main === module) {
  createProxy().listen(7998, "0.0.0.0", () => console.log("zvec-grep owned proxy :7998 -> :7999"));
}
module.exports = { createProxy };
