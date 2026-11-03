import { defineConfig } from 'vitest/config';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import path from 'path';

// Svelte component tests in src/__tests__/ and lib/store tests in __tests__/.
// `css: 'injected'` makes the compiler put each component's <style> into the
// document at mount time. Without it jsdom has NO component CSS at all, so a
// test cannot tell a pane that owns its scroll from one that does not:
// getComputedStyle() would report every property as ''. Injected styles are the
// only way these tests assert the real box chain (overflow-y, min-height,
// flex-shrink) instead of grepping the source for declarations.
export default defineConfig({
  plugins: [svelte({ hot: false, compilerOptions: { css: 'injected' } })],
  resolve: {
    alias: {
      $lib: path.resolve('./src/lib'),
      $stores: path.resolve('./src/stores'),
    },
    // Svelte 5 ships separate client/server entries; without the browser
    // condition vitest resolves the SERVER entry, whose mount() throws
    // lifecycle_function_unavailable in every component test.
    conditions: ['browser'],
  },
  test: {
    environment: 'jsdom',
    include: [
      'src/__tests__/**/*.test.{js,ts}',
      '__tests__/**/*.test.{js,ts}',
    ],
    globals: true,
  },
});
