// The loopback administration surface is never exposed by the network proxy.
// Use the installed upstream client so diagnostics include watcher jobs too.
import { DaemonClient } from "/usr/local/lib/node_modules/@zvec/zvec-grep/dist/client/daemon-client.js";
const client = new DaemonClient({serverUrl: "http://127.0.0.1:7999/mcp"});
try {
  console.log(JSON.stringify(await client.callTool("zvec_grep_server_status", {})));
} catch (error) {
  console.error(String(error));
  process.exitCode = 1;
}
