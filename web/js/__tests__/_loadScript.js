import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const JS_DIR = path.resolve(__dirname, "..");

/**
 * Load an IIFE-style SPA script (e.g. "utils.js") into the current jsdom
 * global, exactly as a <script src> tag would. The script assigns to
 * window.AItelier; tests then read the namespace back off window.
 *
 * @param {string} relName — file name relative to web/js (e.g. "router.js")
 */
export function loadScript(relName) {
  const src = fs.readFileSync(path.join(JS_DIR, relName), "utf8");
  // Run in global scope so `window`, `document`, `setTimeout` resolve to the
  // jsdom globals and `window.AItelier` lands on the shared window object.
  // eslint-disable-next-line no-new-func
  new Function(src).call(globalThis);
}
