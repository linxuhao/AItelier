/** The built-in ranked index (plugin step 11, design 3.5): BM25 over the local memory log.
 *
 * Tokens: runs of Latin letters/digits are words, lower-cased; a run of CJK characters
 * gives its adjacent pairs (bigrams), and every CJK character also gives a unigram token,
 * which only a single-character query uses. Punctuation is dropped. k1 = 1.2, b = 0.75;
 * equal scores order newest first. The inverted index lives in memory. */

const CJK = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]/u;
const RUN = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]+|[\p{L}\p{N}]+/gu;
const UNI = '\u0001'; // marks a CJK unigram token, so it never equals a word

/** Tokens of a document: words, CJK bigrams and CJK unigrams. */
export function documentTokens(text) {
  const out = [];
  for (const run of String(text).toLowerCase().match(RUN) ?? []) {
    if (!CJK.test(run)) { out.push(run); continue; }
    const chars = [...run];
    for (const ch of chars) out.push(UNI + ch);
    for (let i = 0; i + 1 < chars.length; i++) out.push(chars[i] + chars[i + 1]);
  }
  return out;
}

/** Tokens of a query: words, CJK bigrams; a CJK run of one character asks for its unigram. */
export function queryTokens(text) {
  const out = [];
  for (const run of String(text).toLowerCase().match(RUN) ?? []) {
    if (!CJK.test(run)) { out.push(run); continue; }
    const chars = [...run];
    if (chars.length === 1) { out.push(UNI + chars[0]); continue; }
    for (let i = 0; i + 1 < chars.length; i++) out.push(chars[i] + chars[i + 1]);
  }
  return [...new Set(out)];
}

/** The index. Memory is the budget (design 3.3: 100,000 entries within 150 MB), so the
 *  postings are packed typed arrays, the text is not kept here (the caller has it), and a
 *  removed document is a tombstone until a quarter of the index is tombstones, when the
 *  postings are rewritten without them. */
export class Bm25Index {
  constructor({ k1 = 1.2, b = 0.75 } = {}) {
    this.k1 = k1; this.b = b;
    this.postings = new Map(); // token -> { pairs: Int32Array [doc, tf, doc, tf, ...], n }
    this.df = new Map();       // token -> live document count
    this.ids = [];             // doc number -> id (undefined once removed)
    this.ats = [];             // doc number -> at
    this.lengths = [];         // doc number -> token count
    this.numbers = new Map();  // id -> doc number
    this.totalLength = 0;
    this.removed = 0;
  }

  get size() { return this.numbers.size; }

  add(id, text, at = '') {
    if (this.numbers.has(id)) return;
    const tokens = documentTokens(text);
    const tf = new Map();
    for (const t of tokens) tf.set(t, (tf.get(t) ?? 0) + 1);
    const doc = this.ids.length;
    this.ids.push(id); this.ats.push(at); this.lengths.push(tokens.length);
    this.numbers.set(id, doc);
    for (const [t, n] of tf) {
      let p = this.postings.get(t);
      // One interleaved array per token, starting at one pair: most tokens are rare, and
      // each typed array costs an object, so two arrays per token doubled the overhead.
      if (!p) this.postings.set(t, p = { pairs: new Int32Array(2), n: 0 });
      if (p.n * 2 === p.pairs.length) { const grown = new Int32Array(p.pairs.length * 2); grown.set(p.pairs); p.pairs = grown; }
      p.pairs[p.n * 2] = doc; p.pairs[p.n * 2 + 1] = n; p.n++;
      this.df.set(t, (this.df.get(t) ?? 0) + 1);
    }
    this.totalLength += tokens.length;
  }

  /** `text` is the removed document's text (its tokens give the counts to take back). */
  remove(id, text) {
    const doc = this.numbers.get(id);
    if (doc === undefined) return false;
    for (const t of new Set(documentTokens(text))) {
      const left = (this.df.get(t) ?? 1) - 1;
      if (left > 0) this.df.set(t, left); else this.df.delete(t);
    }
    this.totalLength -= this.lengths[doc];
    this.ids[doc] = undefined;
    this.numbers.delete(id);
    if (++this.removed > 1000 && this.removed * 4 > this.ids.length) this.#compact();
    return true;
  }

  #compact() {
    for (const [t, p] of this.postings) {
      let w = 0;
      for (let r = 0; r < p.n; r++) if (this.ids[p.pairs[r * 2]] !== undefined) { p.pairs[w * 2] = p.pairs[r * 2]; p.pairs[w * 2 + 1] = p.pairs[r * 2 + 1]; w++; }
      if (w) p.n = w; else this.postings.delete(t);
    }
    this.removed = 0;
  }

  /** [{id, score, at}], best first; equal scores newest first. */
  search(query, topK = 10) {
    const n = this.numbers.size;
    if (!n) return [];
    const avgdl = this.totalLength / n || 1;
    const scores = new Map();
    for (const t of queryTokens(query)) {
      const p = this.postings.get(t), df = this.df.get(t);
      if (!p || !df) continue;
      const idf = Math.log(1 + (n - df + 0.5) / (df + 0.5));
      for (let i = 0; i < p.n; i++) {
        const doc = p.pairs[i * 2];
        if (this.ids[doc] === undefined) continue;
        const tf = p.pairs[i * 2 + 1], dl = this.lengths[doc];
        scores.set(doc, (scores.get(doc) ?? 0) + idf * (tf * (this.k1 + 1)) / (tf + this.k1 * (1 - this.b + this.b * dl / avgdl)));
      }
    }
    return [...scores].map(([doc, score]) => ({ id: this.ids[doc], score, at: this.ats[doc] }))
      .sort((x, y) => y.score - x.score || (y.at > x.at ? 1 : y.at < x.at ? -1 : 0))
      .slice(0, topK);
  }
}

/** The RankedIndex (design 3.2) over the local index: `ref` is the local entry id and
 *  `textOf(id)` the entry's text. The store feeds the index itself (it owns the log), so
 *  add here is a no-op. */
export function bm25Ranked(index, textOf) {
  return {
    name: 'bm25 · 本地索引',
    local: true,
    async add() {},
    async search(query, topK) {
      return index.search(query, topK).map(h => ({ text: textOf(h.id) ?? '', score: Math.round(h.score * 100) / 100, at: h.at, ref: h.id }));
    },
  };
}
