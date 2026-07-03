import svelte from 'eslint-plugin-svelte';
import tsParser from '@typescript-eslint/parser';

export default [
  { ignores: ['dist/', 'node_modules/'] },
  // Flat-config preset (array): wires svelte-eslint-parser for *.svelte
  // and the plugin's recommended rules. The old deep import
  // 'eslint-plugin-svelte/configs/recommended.js' does not exist in the
  // published package layout (configs live under lib/ with no exports map).
  ...svelte.configs['flat/recommended'],
  {
    // <script lang="ts"> blocks need a TS parser inside the svelte parser.
    files: ['**/*.svelte'],
    languageOptions: {
      parserOptions: { parser: tsParser },
    },
    rules: {
      // Every {@html} site renders DOMPurify-sanitized output (lib/markdown.ts).
      'svelte/no-at-html-tags': 'off',
      // Pre-existing unkeyed each blocks; visible but non-blocking. Note some
      // lists key by position ON PURPOSE (see Trace.svelte: duplicate seq
      // values made a seq-keyed each fatal).
      'svelte/require-each-key': 'warn',
      // False-positives on non-reactive function-local Sets/Maps.
      'svelte/prefer-svelte-reactivity': 'warn',
    },
  },
  {
    files: ['**/*.{js,ts}'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      parser: tsParser,
    },
    rules: {
      'no-unused-vars': 'warn',
      // no-undef is redundant under TypeScript (tsc catches undefined
      // identifiers) and false-positives on browser globals here.
      'no-undef': 'off',
    },
  },
];
