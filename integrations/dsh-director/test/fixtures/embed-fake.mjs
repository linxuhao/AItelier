/** A deterministic stand-in for llama-server --embedding (design-steward 3.8.4 item 6): the
 *  OpenAI-compatible POST /v1/embeddings on 127.0.0.1, an ephemeral port. A text's vector is
 *  derived from a hash, so the same text always gets the same unit vector; `knobs` make it
 *  misbehave:
 *    vectors   Map text -> number[] (an exact text's vector, before anything else)
 *    concepts  [needle, seed][]: a text containing `needle` gets the vector of `seed`
 *    delayMs   answer late;  hang: accept and never answer;  status: answer with that HTTP status
 *    tooLongIf: a predicate over the input: true gets llama.cpp's 400 "too large"
 *    dims      answer with this many dimensions (a wrong-dims server)
 *  `requests` records every request ({input, model}). Not a test: tests import it. */
import { createServer } from 'node:http';
import { createHash } from 'node:crypto';

export function hashVector(seed, dims) {
  const out = new Array(dims);
  let block = Buffer.alloc(0), i = 0, counter = 0;
  while (i < dims) {
    if (!block.length) block = createHash('sha256').update(`${seed}\0${counter++}`).digest();
    out[i++] = block[0] / 127.5 - 1;
    block = block.subarray(1);
  }
  return unit(out);
}

export function unit(values) {
  const norm = Math.sqrt(values.reduce((n, x) => n + x * x, 0)) || 1;
  return values.map(x => x / norm);
}

/** A unit vector along axis `i`, or along the sum of several axes. */
export function axis(dims, ...axes) {
  const v = new Array(dims).fill(0);
  for (const a of axes) v[a] += 1;
  return unit(v);
}

export async function fakeEmbedServer({ dims = 1024 } = {}) {
  const knobs = { vectors: new Map(), concepts: [], delayMs: 0, hang: false, status: 0, tooLongIf: () => false, dims: undefined };
  const requests = [];
  const sockets = new Set();
  const vectorOf = (text, n) => {
    if (knobs.vectors.has(text)) return knobs.vectors.get(text);
    const concept = knobs.concepts.find(([needle]) => text.includes(needle));
    return hashVector(concept ? concept[1] : text, n);
  };
  const server = createServer((req, res) => {
    let body = '';
    req.setEncoding('utf8');
    req.on('data', chunk => { body += chunk; });
    req.on('end', async () => {
      let parsed = {};
      try { parsed = JSON.parse(body); } catch {}
      requests.push({ url: req.url, input: parsed.input, model: parsed.model });
      if (knobs.hang) return; // the socket stays open, never answered
      if (knobs.delayMs) await new Promise(resolve => setTimeout(resolve, knobs.delayMs));
      res.setHeader('content-type', 'application/json');
      if (req.url !== '/v1/embeddings' || typeof parsed.input !== 'string') { res.writeHead(404); res.end('{}'); return; }
      if (knobs.status) { res.writeHead(knobs.status); res.end(JSON.stringify({ error: { code: knobs.status, message: 'injected failure' } })); return; }
      if (knobs.tooLongIf(parsed.input)) {
        res.writeHead(400);
        res.end(JSON.stringify({ error: { code: 400, message: 'input is too large to process. increase the physical batch size', type: 'invalid_request_error' } }));
        return;
      }
      const n = knobs.dims ?? dims;
      res.end(JSON.stringify({ object: 'list', model: parsed.model,
        data: [{ object: 'embedding', index: 0, embedding: vectorOf(parsed.input, n) }] }));
    });
  });
  server.on('connection', socket => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)); });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address();
  return {
    url: `http://127.0.0.1:${port}`, port, knobs, requests,
    embeds: () => requests.filter(r => r.url === '/v1/embeddings').length,
    async close() {
      for (const socket of sockets) socket.destroy();
      await new Promise(resolve => server.close(resolve));
    },
  };
}

/** A loopback port nothing listens on (connections are refused). */
export async function refusedPort() {
  const server = createServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address();
  await new Promise(resolve => server.close(resolve));
  return port;
}
