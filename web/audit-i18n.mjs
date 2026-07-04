#!/usr/bin/env node

/**
 * audit-i18n.mjs
 *
 * Standalone audit script that verifies every t() key used in Svelte
 * components has a corresponding translation in all 8 languages.
 *
 * Usage: node audit-i18n.mjs
 *   - Exit 0: All keys present in all 8 languages.
 *   - Exit 1: One or more keys missing (details printed to stdout).
 */

import { readFileSync, readdirSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const WEB_ROOT = __dirname;
const VIEWS_DIR = join(WEB_ROOT, 'src', 'views');
const I18N_FILE = join(WEB_ROOT, 'src', 'lib', 'i18n.svelte.ts');

const LANGUAGES = ['en', 'zh-CN', 'zh-TW', 'ja', 'ko', 'fr', 'de', 'es'];

/**
 * Extract all t() key strings from a Svelte component file.
 * Handles both single-quote and double-quote: t('key') and t("key").
 */
function extractComponentKeys(content) {
  const keys = new Set();
  const regex = /t\(['"]([^'"]+)['"]\)/g;
  let match;
  while ((match = regex.exec(content)) !== null) {
    keys.add(match[1]);
  }
  return keys;
}

/**
 * Find the position past the opening brace of a language block,
 * given the start of the language declaration line.
 *
 * Returns the index of the character just after the opening `{`,
 * or -1 if not found.
 */
function findBlockStart(content, startIdx) {
  // Scan from startIdx for the opening `{`
  for (let i = startIdx; i < content.length; i++) {
    if (content[i] === '{') {
      return i + 1;
    }
  }
  return -1;
}

/**
 * Find the matching closing brace for a block, skipping braces
 * inside single-quoted strings (to handle {id}, {n} placeholders).
 *
 * Returns the index of the matching `}`.
 */
function findBlockEnd(content, startPos) {
  let depth = 1;
  let inString = false;
  let escape = false;

  for (let i = startPos; i < content.length; i++) {
    const ch = content[i];

    if (inString) {
      if (escape) {
        escape = false;
        continue;
      }
      if (ch === '\\') {
        escape = true;
        continue;
      }
      if (ch === "'") {
        inString = false;
      }
      continue;
    }

    // Not in a string
    if (ch === "'") {
      inString = true;
      escape = false;
      continue;
    }

    if (ch === '{') {
      depth++;
    } else if (ch === '}') {
      depth--;
      if (depth === 0) {
        return i;
      }
    }
  }

  return -1;
}

/**
 * Extract the raw text content of a language block (between `{` and `}`).
 */
function extractLanguageBlock(content, lang) {
  const escapedLang = lang.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

  // Try quoted key first: 'zh-CN':  or "zh-CN":
  let re = new RegExp(`'${escapedLang}'\\s*:\\s*\\{`);
  let match = re.exec(content);

  if (!match) {
    // Try unquoted: en:  or ja:
    re = new RegExp(`\\b${escapedLang}\\s*:\\s*\\{`);
    match = re.exec(content);
  }

  if (!match) return null;

  const blockStart = match.index + match[0].length - 1; // point at the `{`
  const blockEnd = findBlockEnd(content, blockStart + 1);

  if (blockEnd === -1) return null;

  return content.slice(blockStart + 1, blockEnd);
}

/**
 * Extract all keys from a language block text.
 */
function extractKeysFromBlock(blockContent) {
  const keys = new Set();
  const keyRegex = /['"]([^'"]+)['"]\s*:/g;
  let m;
  while ((m = keyRegex.exec(blockContent)) !== null) {
    // Skip keys that look like numeric property names
    keys.add(m[1]);
  }
  return keys;
}

/**
 * Parse the translations object from i18n.svelte.ts.
 *
 * Returns { language -> Set<key> }
 */
function extractTranslationKeys(content) {
  const result = {};

  for (const lang of LANGUAGES) {
    const blockContent = extractLanguageBlock(content, lang);
    result[lang] = blockContent
      ? extractKeysFromBlock(blockContent)
      : new Set();
  }

  return result;
}

function main() {
  // ── Step 1: Extract component keys ──
  const componentKeys = new Set();
  const viewFiles = readdirSync(VIEWS_DIR).filter(f => f.endsWith('.svelte'));

  if (viewFiles.length === 0) {
    console.error('❌ No .svelte files found in', VIEWS_DIR);
    process.exit(1);
  }

  for (const file of viewFiles) {
    const filePath = join(VIEWS_DIR, file);
    const content = readFileSync(filePath, 'utf-8');
    const keys = extractComponentKeys(content);
    for (const key of keys) {
      componentKeys.add(key);
    }
  }

  // ── Step 2: Extract translation keys ──
  const i18nContent = readFileSync(I18N_FILE, 'utf-8');
  const translationKeys = extractTranslationKeys(i18nContent);

  // ── Step 3: Cross-reference ──
  const missing = {};

  for (const lang of LANGUAGES) {
    const langKeys = translationKeys[lang];
    if (!langKeys) {
      missing[lang] = Array.from(componentKeys);
      continue;
    }

    const missingForLang = [];
    for (const key of componentKeys) {
      if (!langKeys.has(key)) {
        missingForLang.push(key);
      }
    }

    if (missingForLang.length > 0) {
      missing[lang] = missingForLang;
    }
  }

  // ── Step 4: Report ──
  const totalMissing = Object.keys(missing).length;

  if (totalMissing === 0) {
    console.log(`✅ All ${componentKeys.size} keys present in all ${LANGUAGES.length} languages.`);
    process.exit(0);
  } else {
    console.log('❌ MISSING KEYS:');
    for (const lang of LANGUAGES) {
      if (missing[lang]) {
        console.log(`  - ${lang}:`);
        for (const key of missing[lang]) {
          console.log(`    - ${key}`);
        }
      }
    }
    process.exit(1);
  }
}

main();
