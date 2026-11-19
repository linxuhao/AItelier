/** The login QR drawn by the bridge (qr-svg.mjs) with the real vendored encoder: what it
 *  draws decodes back to the payload (jsQR, the decoder puppet-neo itself uses), it is a
 *  plain SVG of one white square and one black path, and the checked-in fixture is
 *  exactly its output (tests/unit/test_steward_channels.py checks that the backend
 *  accepts the fixture). */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { qrModules, qrSvg, QUIET_ZONE, MAX_SVG_BYTES } from './qr-svg.mjs';

const require = createRequire(import.meta.url);
const jsQR = require('jsqr');
const FIXTURE_PAYLOAD = 'https://login.weixin.qq.com/l/TEST-NOT-A-LOGIN-0123456789';

/** Rasterizes what qrSvg draws (its white square, then each stroke `Mx y.5h<run>`) at
 *  `scale` pixels per module and decodes it. */
function decode(svg, scale = 4) {
  const size = Number(/viewBox="0 0 (\d+) \1"/.exec(svg)[1]);
  const side = size * scale;
  const pixels = new Uint8ClampedArray(side * side * 4).fill(255);
  const d = /<path stroke="#000" d="([^"]*)"\/>/.exec(svg)[1];
  const strokes = [...d.matchAll(/M(\d+) (\d+)\.5h(\d+)/g)];
  assert.equal(strokes.map(m => m[0]).join(''), d, 'the path is nothing but strokes');
  for (const [, x, y, run] of strokes)
    for (let py = Number(y) * scale; py < (Number(y) + 1) * scale; py++)
      for (let px = Number(x) * scale; px < (Number(x) + Number(run)) * scale; px++)
        pixels.fill(0, (py * side + px) * 4, (py * side + px) * 4 + 3);
  return jsQR(pixels, side, side)?.data;
}

test('the drawn QR decodes back to the payload, also one that is not ASCII', () => {
  for (const payload of [FIXTURE_PAYLOAD, 'https://login.weixin.qq.com/l/Ab_9-x?q=1', '用要绑定的微信小号扫码登录 https://x.example/l/1'])
    assert.equal(decode(qrSvg(payload)), payload);
});

test('the image is one plain SVG: a white square, one black path, a quiet zone, nothing that runs or links', () => {
  const svg = qrSvg(FIXTURE_PAYLOAD);
  const modules = qrModules(FIXTURE_PAYLOAD).length;
  assert.match(svg, new RegExp(`^<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${modules + 2 * QUIET_ZONE} ${modules + 2 * QUIET_ZONE}" shape-rendering="crispEdges"><path fill="#fff" d="M0 0h\\d+v\\d+H0z"/><path stroke="#000" d="[M0-9 .h]+"/></svg>$`));
  assert.doesNotMatch(svg, /script|foreignobject|href|\son\w*=|<[!?]/i);
  assert.ok(!svg.includes(FIXTURE_PAYLOAD), 'the payload is drawn, not written into the image');
});

test('the checked-in fixture is exactly what the bridge draws', () => {
  assert.equal(readFileSync(new URL('./test-fixtures/login-qr.svg', import.meta.url), 'utf8'), qrSvg(FIXTURE_PAYLOAD));
});

test('a payload whose drawing would exceed the backend bound is not drawn', () => {
  const payload = 'x'.repeat(2000);
  assert.equal(qrSvg(payload), undefined);
  assert.ok(qrModules(payload).length > 150, 'it is a real, large code');
  assert.ok(Buffer.byteLength(qrSvg('y'.repeat(200))) < MAX_SVG_BYTES);
});
