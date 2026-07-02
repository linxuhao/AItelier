import svelte from 'eslint-plugin-svelte';
import tsParser from '@typescript-eslint/parser';
import globals from 'globals';

export default [
  { ignores: ['dist/', 'node_modules/'] },
  ...svelte.configs.recommended,
  {
    files: ['**/*.svelte'],
    languageOptions: {
      globals: { ...globals.browser },
      parserOptions: {
        parser: tsParser,
      },
    },
  },
  {
    files: ['**/*.{js,ts}'],
    languageOptions: {
      parser: tsParser,
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: { ...globals.browser },
    },
    rules: {
      'no-unused-vars': 'warn',
      'no-undef': 'error',
    },
  },
  {
    // TS ambient types (e.g. RequestInit) trip core no-undef; tsc covers this.
    files: ['**/*.ts'],
    rules: {
      'no-undef': 'off',
    },
  },
];
