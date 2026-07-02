import { defineConfig } from 'vitest/config';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import path from 'path';

// Svelte component tests in src/__tests__/ and lib/store tests in __tests__/.
export default defineConfig({
  plugins: [svelte({ hot: false })],
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
