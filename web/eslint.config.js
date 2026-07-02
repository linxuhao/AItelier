import svelteRecommended from 'eslint-plugin-svelte/configs/recommended.js';
import svelteParser from 'svelte-eslint-parser';

export default [
  { ignores: ['dist/', 'node_modules/'] },
  {
    files: ['**/*.svelte'],
    languageOptions: {
      parser: svelteParser,
    },
    ...svelteRecommended,
  },
  {
    files: ['**/*.{js,ts}'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
    },
    rules: {
      'no-unused-vars': 'warn',
      'no-undef': 'error',
    },
  },
];
