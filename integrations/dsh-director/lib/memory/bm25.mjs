/** The built-in ranked index (plugin step 11, design 3.5): BM25 over the local memory log.
 *
 * Tokens: a run of letters/digits is cut at the script boundary first, so a Latin word glued
 * to Chinese stays one word (`用memory系统` gives `memory`); a non-CJK segment is a word,
 * lower-cased; a CJK segment gives its adjacent pairs (bigrams), and every CJK character also
 * gives a unigram token, which only a single-character query uses. Punctuation is dropped,
 * and the prolonged sound mark stays inside its kana word (`カタカナトーク` gives `トー`).
 * k1 = 1.2, b = 0.75; equal scores order newest first. The inverted index lives in memory. */

// `\p{L}` contains Han, so a run may mix scripts: the two capture groups below cut it into
// maximal CJK and maximal non-CJK segments, and the group that matched says which it is.
// Classifying a whole run by "contains any CJK" would swallow a glued Latin word.
// The kana prolonged sound mark (U+30FC, and its halfwidth form U+FF70) is listed by hand
// because its Script is Common, yet it lengthens the kana in front of it (コーヒー, トーク):
// cut off, it became a word of its own and the bigrams across it (トー, ーク) were lost.
// U+30FB KATAKANA MIDDLE DOT is deliberately absent: it is an interpunct between words
// (パン・ケーキ), punctuation already dropped before a run is built, so it never gets here.
const CJK_CHAR = '\\p{Script=Han}\\p{Script=Hiragana}\\p{Script=Katakana}\\p{Script=Hangul}\\u30FC\\uFF70';
const WORD = /[\p{L}\p{N}]+/gu;
const SEGMENT = new RegExp(`([${CJK_CHAR}]+)|([^${CJK_CHAR}]+)`, 'gu');
const UNI = '\u0001'; // marks a CJK unigram token, so it never equals a word

/** One letter/digit run as [segment, isCjk] pairs: maximal CJK and maximal non-CJK pieces. */
function segments(run) {
  const parts = [];
  for (const m of run.matchAll(SEGMENT)) parts.push(m[1] !== undefined ? [m[1], true] : [m[2], false]);
  return parts;
}

/** Tokens of a document: words, CJK bigrams and CJK unigrams. */
export function documentTokens(text) {
  const out = [];
  for (const run of String(text).toLowerCase().match(WORD) ?? []) {
    for (const [segment, cjk] of segments(run)) {
      if (!cjk) { out.push(segment); continue; }
      const chars = [...segment];
      for (const ch of chars) out.push(UNI + ch);
      for (let i = 0; i + 1 < chars.length; i++) out.push(chars[i] + chars[i + 1]);
    }
  }
  return out;
}

/** Tokens of a query: words, CJK bigrams; a CJK segment of one character asks for its unigram. */
export function queryTokens(text) {
  const out = [];
  for (const run of String(text).toLowerCase().match(WORD) ?? []) {
    for (const [segment, cjk] of segments(run)) {
      if (!cjk) { out.push(segment); continue; }
      const chars = [...segment];
      if (chars.length === 1) { out.push(UNI + chars[0]); continue; }
      for (let i = 0; i + 1 < chars.length; i++) out.push(chars[i] + chars[i + 1]);
    }
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

  /** `tokens` is the document's token list as the caller built it (design 3.5: the text's
   *  tokens plus its date words); remove takes back exactly the same list. The index never
   *  tokenizes a document itself, so what was added is what is taken away. */
  add(id, tokens, at = '') {
    if (this.numbers.has(id)) return;
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

  /** `tokens` is the list `add` was given for this document (its counts are taken back). */
  remove(id, tokens) {
    const doc = this.numbers.get(id);
    if (doc === undefined) return false;
    for (const t of new Set(tokens)) {
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

  /** [{id, score, at}], best first; equal scores newest first (then the order first scored).
   *  A date word is in nearly every entry (design 3.8.1: 2026, 月, 日, the month), so a date
   *  query scores almost every document: the scores go into one slot per document and only
   *  the best topK are kept, by a bounded heap - a Map per hit and a sort of all of them
   *  blocked the loop for 25-60 ms at 100k entries (the budget is 20 ms, design 3.8.2). */
  search(query, topK = 10) {
    const n = this.numbers.size;
    if (!n || !(topK > 0)) return [];
    const avgdl = this.totalLength / n || 1;
    const scores = new Float64Array(this.ids.length);
    const touched = [];
    for (const t of queryTokens(query)) {
      const p = this.postings.get(t), df = this.df.get(t);
      if (!p || !df) continue;
      const idf = Math.log(1 + (n - df + 0.5) / (df + 0.5));
      for (let i = 0; i < p.n; i++) {
        const doc = p.pairs[i * 2];
        if (this.ids[doc] === undefined) continue;
        const tf = p.pairs[i * 2 + 1], dl = this.lengths[doc];
        if (scores[doc] === 0) touched.push(doc); // every term adds > 0 (idf > 0, tf >= 1)
        scores[doc] += idf * (tf * (this.k1 + 1)) / (tf + this.k1 * (1 - this.b + this.b * dl / avgdl));
      }
    }
    // worse(a, b): positions in `touched`; a ranks after b.
    const ats = this.ats;
    const worse = (a, b) => {
      const x = scores[touched[a]], y = scores[touched[b]];
      if (x !== y) return x < y;
      const p = ats[touched[a]], q = ats[touched[b]];
      return p !== q ? p < q : a > b;
    };
    const heap = []; // the kept positions, worst at the root
    const down = i => {
      for (;;) {
        const l = 2 * i + 1, r = l + 1;
        let w = i;
        if (l < heap.length && worse(heap[l], heap[w])) w = l;
        if (r < heap.length && worse(heap[r], heap[w])) w = r;
        if (w === i) return;
        [heap[i], heap[w]] = [heap[w], heap[i]]; i = w;
      }
    };
    // Last scored first: documents are numbered in log order, so that is mostly newest first,
    // and among equal scores (a date word alone) the rest is turned away by one comparison.
    for (let j = touched.length - 1; j >= 0; j--) {
      if (heap.length < topK) {
        heap.push(j);
        for (let i = heap.length - 1; i > 0;) { const up = (i - 1) >> 1; if (!worse(heap[i], heap[up])) break; [heap[i], heap[up]] = [heap[up], heap[i]]; i = up; }
      } else if (worse(heap[0], j)) { heap[0] = j; down(0); }
    }
    return heap.sort((a, b) => (worse(a, b) ? 1 : worse(b, a) ? -1 : 0))
      .map(j => ({ id: this.ids[touched[j]], score: scores[touched[j]], at: ats[touched[j]] }));
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
