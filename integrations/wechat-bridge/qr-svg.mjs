/** The login QR as an SVG image, drawn here from the QR payload so the owner can scan it
 *  from the dashboard.
 *
 *  The encoder is the QRCode that qrcode-terminal (0.12.0, pinned by puppet-neo, so
 *  already in this image) ships in its vendor directory: no package of its own. The
 *  image is a white square and one black path, nothing else, so it passes the
 *  backend's check of a reported QR (core/state_channels.py check_qr_svg). The payload
 *  is a live login token: nothing here logs it. */
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const QRCode = require('qrcode-terminal/vendor/QRCode/index.js');
const QRErrorCorrectLevel = require('qrcode-terminal/vendor/QRCode/QRErrorCorrectLevel.js');

/** White modules around the code, as the QR standard asks for. */
export const QUIET_ZONE = 4;
/** The backend's bound on a reported QR image (QR_SVG_MAX_BYTES). */
export const MAX_SVG_BYTES = 64 * 1024;

/** The dark/light modules of `text` (UTF-8, byte mode, error correction M), row by row. */
export function qrModules(text) {
  const qr = new QRCode(-1, QRErrorCorrectLevel.M);
  // The vendored encoder writes one byte per UTF-16 unit: hand it the UTF-8 bytes.
  qr.addData(Buffer.from(String(text), 'utf8').toString('latin1'));
  qr.make();
  return qr.modules.map(row => row.map(Boolean));
}

/** `text` as an SVG document, or undefined when it cannot be drawn within MAX_SVG_BYTES.
 *  Each run of dark modules in a row is one stroke of width 1. */
export function qrSvg(text) {
  const modules = qrModules(text);
  const size = modules.length + 2 * QUIET_ZONE;
  let d = '';
  modules.forEach((row, y) => {
    for (let x = 0; x < row.length; x++) {
      if (!row[x]) continue;
      let run = 1;
      while (row[x + run]) run++;
      d += `M${x + QUIET_ZONE} ${y + QUIET_ZONE}.5h${run}`;
      x += run - 1;
    }
  });
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${size} ${size}" shape-rendering="crispEdges">`
    + `<path fill="#fff" d="M0 0h${size}v${size}H0z"/><path stroke="#000" d="${d}"/></svg>`;
  return Buffer.byteLength(svg) <= MAX_SVG_BYTES ? svg : undefined;
}
