import { defineConfig } from "vitest/config";

// The SPA scripts are plain browser globals (IIFEs attaching to
// window.AItelier), loaded via <script> tags — not ES modules. jsdom gives
// them a window/document to attach to; tests read the helpers back off
// window.AItelier. See js/__tests__/_loadScript.js.
export default defineConfig({
  test: {
    environment: "jsdom",
    include: ["js/__tests__/**/*.test.js"],
    globals: true,
  },
});
