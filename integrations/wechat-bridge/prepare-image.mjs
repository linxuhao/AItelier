/** Prepare a picture for the fork's one image carrier.
 *
 * The fork sends a file whose name carries an image extension with
 * `Operation::SendImage` (0001-incoming-message-types.patch, `carrier()`). The crate it
 * calls, wechat-protocol 2.2.0 (`media.rs`, verified against the pinned upstream Cargo.lock:
 * SHA256 31dd60cd137a312e5b531d8ea9ea4115939e64284a97d261c33a2be572308ff7), refuses every
 * JPEG variant bigger than 65520 bytes (`jpeg_dimensions`: "JPEG variant outside small-object
 * profile"; `PreparedImageUpload::validate`: `original.plaintext_bytes <= 65520`), decodes
 * only JPEG (`Expected JPEG variant`) at most 4096 x 4096, and - because the fork passes the
 * one original as the standard AND thumbnail variants - requires that same JPEG's dimensions
 * to be at most 320 x 320 (`Invalid JPEG variant sizes`). A generated PNG of hundreds of KB
 * can therefore never be a bubble as it stands, and neither can a larger JPEG.
 *
 * This module makes a bubble out of such a picture: decode the real bytes, composite and
 * downscale to at most 320 x 320 (the crate's own thumbnail bound), re-encode as a baseline
 * JPEG and lower the quality until the bytes fit the small-object profile. It never guesses
 * from a file name: undecodable bytes come back undefined and go out as a truthful file.
 * A JPEG that already fits the carrier is left byte for byte as it arrived.
 *
 * PNG is decoded here with node:zlib (no extra dependency); JPEG is encoded/decoded by the
 * pinned, pure-JS jpeg-js 0.4.4 under vendor/jpeg-js (BSD-3-Clause, LICENSE there). That copy
 * came out of the bridge image's own node_modules, the tree npm ci installs from the same
 * committed package-lock as the running bridge, so the test suite needs no network or npm
 * install. It is vendored rather than imported by name because the repository's test image
 * does not carry the bridge's node_modules.
 */
import { inflateSync } from 'node:zlib';
import jpeg from './vendor/jpeg-js/index.js';

/** wechat-protocol 2.2.0's small-object ceiling, in bytes (not the old 64 KiB guess). */
export const SMALL_OBJECT_BYTES = 65_520;
/** Its thumbnail bound; the fork sends the original as every variant, so it bounds the bubble. */
export const SMALL_OBJECT_EDGE = 320;
const MAX_DECODE_PIXELS = 16 * 1024 * 1024;
const MAX_INFLATE_BYTES = 64 * 1024 * 1024;
const QUALITIES = [85, 70, 55, 40, 25, 10, 5];
const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
const SOF = new Set([0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf]);
const isJpeg = bytes => bytes.length >= 3 && bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff;

/** The dimensions a JPEG states in its own SOF header, or undefined when it states none.
 *  Header only: it keeps a plain small JPEG untouched and finds the large ones to prepare. */
export function jpegEdges(bytes) {
  if (!isJpeg(bytes)) return undefined;
  for (let at = 2; at + 9 <= bytes.length;) {
    if (bytes[at] !== 0xff) { at++; continue; }
    const marker = bytes[at + 1];
    if (marker === 0xff) { at++; continue; }
    if (marker === 0x01 || marker === 0xd8 || (marker >= 0xd0 && marker <= 0xd7)) { at += 2; continue; }
    if (marker === 0xd9 || marker === 0xda) break;
    const length = bytes.readUInt16BE(at + 2);
    if (length < 2 || at + 2 + length > bytes.length) return undefined;
    if (SOF.has(marker)) {
      const height = bytes.readUInt16BE(at + 5), width = bytes.readUInt16BE(at + 7);
      return width && height ? { width, height } : undefined;
    }
    at += 2 + length;
  }
  return undefined;
}

/** Whether the bridge must prepare these bytes before it can ask for an image bubble. */
export function needsPreparation(bytes, limits = {}) {
  const ceiling = Math.min(limits.imageMaxBytes ?? SMALL_OBJECT_BYTES, SMALL_OBJECT_BYTES);
  if (!isJpeg(bytes) || bytes.length > ceiling) return true;
  const edges = jpegEdges(bytes);
  return Boolean(edges && Math.max(edges.width, edges.height) > SMALL_OBJECT_EDGE);
}

const paeth = (left, up, upperLeft) => {
  const p = left + up - upperLeft;
  const a = Math.abs(p - left), b = Math.abs(p - up), c = Math.abs(p - upperLeft);
  return a <= b && a <= c ? left : b <= c ? up : upperLeft;
};

/** A PNG's pixels as RGBA, for the bit depths and colour types a generated picture uses.
 *  Interlaced, 16-bit and malformed PNGs are refused (undefined at the caller), never guessed. */
export function pngToRgba(bytes) {
  if (bytes.length < 33 || !PNG_SIGNATURE.equals(bytes.subarray(0, 8))) throw new Error('not a PNG');
  let at = 8, width, height, depth, colour, interlace, palette, alpha;
  const idat = [];
  for (; at + 12 <= bytes.length;) {
    const length = bytes.readUInt32BE(at);
    if (length > bytes.length - at - 12) throw new Error('a PNG chunk runs past the file');
    const type = bytes.toString('latin1', at + 4, at + 8);
    const data = bytes.subarray(at + 8, at + 8 + length);
    if (type === 'IHDR') {
      if (length !== 13) throw new Error('bad PNG IHDR');
      width = data.readUInt32BE(0); height = data.readUInt32BE(4);
      depth = data[8]; colour = data[9]; interlace = data[12];
    } else if (type === 'PLTE') palette = data;
    else if (type === 'tRNS') alpha = data;
    else if (type === 'IDAT') idat.push(data);
    else if (type === 'IEND') break;
    at += 12 + length;
  }
  const channels = { 0: 1, 2: 3, 3: 1, 4: 2, 6: 4 }[colour];
  if (!width || !height || depth !== 8 || !channels || interlace !== 0 || !idat.length
    || width * height > MAX_DECODE_PIXELS || (colour === 3 && !palette)) throw new Error('unsupported PNG');
  const stride = width * channels;
  const raw = inflateSync(Buffer.concat(idat), { maxOutputLength: MAX_INFLATE_BYTES });
  if (raw.length < height * (stride + 1)) throw new Error('a PNG scanline is missing');
  const out = new Uint8Array(width * height * 4);
  const previous = new Uint8Array(stride), current = new Uint8Array(stride);
  let pos = 0;
  for (let y = 0; y < height; y++) {
    const filter = raw[pos++];
    for (let i = 0; i < stride; i++) current[i] = raw[pos++];
    if (filter > 4) throw new Error('unknown PNG filter');
    for (let i = 0; i < stride; i++) {
      const left = i >= channels ? current[i - channels] : 0;
      const up = previous[i];
      const upperLeft = i >= channels ? previous[i - channels] : 0;
      const add = filter === 1 ? left : filter === 2 ? up : filter === 3 ? (left + up) >> 1
        : filter === 4 ? paeth(left, up, upperLeft) : 0;
      current[i] = (current[i] + add) & 255;
    }
    for (let x = 0; x < width; x++) {
      const source = x * channels, target = (y * width + x) * 4;
      let r, g, b, a = 255;
      if (colour === 0 || colour === 4) r = g = b = current[source];
      else if (colour === 2 || colour === 6) { r = current[source]; g = current[source + 1]; b = current[source + 2]; }
      else { const index = current[source]; r = palette[index * 3]; g = palette[index * 3 + 1]; b = palette[index * 3 + 2]; }
      if (colour === 4 || colour === 6) a = current[source + channels - 1];
      else if (colour === 3 && alpha && current[source] < alpha.length) a = alpha[current[source]];
      out[target] = r; out[target + 1] = g; out[target + 2] = b; out[target + 3] = a;
    }
    previous.set(current);
  }
  return { data: out, width, height };
}

export function jpegToRgba(bytes) {
  const decoded = jpeg.decode(bytes, { useTArray: true, formatAsRGBA: true });
  if (!decoded?.width || !decoded?.height || decoded.data?.length !== decoded.width * decoded.height * 4)
    throw new Error('the JPEG decoder gave no RGBA picture');
  return { data: decoded.data, width: decoded.width, height: decoded.height };
}

/** Box-filter to at most `edge` on the long side, compositing transparency on white: a JPEG
 *  has no alpha and jpeg-js would otherwise paint transparent pixels with their raw RGB. */
export function resized(image, edge) {
  const scale = Math.min(1, edge / Math.max(image.width, image.height));
  const width = Math.max(1, Math.round(image.width * scale)), height = Math.max(1, Math.round(image.height * scale));
  const out = new Uint8Array(width * height * 4);
  const fromY = y => Math.floor(y * image.height / height), toY = y => Math.max(fromY(y) + 1, Math.floor((y + 1) * image.height / height));
  const fromX = x => Math.floor(x * image.width / width), toX = x => Math.max(fromX(x) + 1, Math.floor((x + 1) * image.width / width));
  for (let y = 0; y < height; y++) {
    const y0 = fromY(y), y1 = toY(y);
    for (let x = 0; x < width; x++) {
      const x0 = fromX(x), x1 = toX(x);
      let r = 0, g = 0, b = 0, a = 0, n = 0;
      for (let sy = y0; sy < y1; sy++) for (let sx = x0; sx < x1; sx++) {
        const at = (sy * image.width + sx) * 4, opacity = image.data[at + 3] / 255;
        r += image.data[at] * opacity + 255 * (1 - opacity);
        g += image.data[at + 1] * opacity + 255 * (1 - opacity);
        b += image.data[at + 2] * opacity + 255 * (1 - opacity);
        a += image.data[at + 3]; n++;
      }
      const target = (y * width + x) * 4;
      out[target] = Math.round(r / n); out[target + 1] = Math.round(g / n);
      out[target + 2] = Math.round(b / n); out[target + 3] = Math.round(a / n);
    }
  }
  return { data: out, width, height };
}

/** Decodable picture -> a real JPEG the crate's small-object profile accepts, or undefined.
 *  `limits.imageMaxBytes` may be tightened by a caller; it is never widened past the crate's. */
export function preparePicture(bytes, limits = {}) {
  if (!Buffer.isBuffer(bytes) || !bytes.length) return undefined;
  const ceiling = Math.min(limits.imageMaxBytes ?? SMALL_OBJECT_BYTES, SMALL_OBJECT_BYTES);
  try {
    const decoded = isJpeg(bytes) ? jpegToRgba(bytes) : pngToRgba(bytes);
    const small = resized(decoded, SMALL_OBJECT_EDGE);
    for (const quality of QUALITIES) {
      const encoded = Buffer.from(jpeg.encode(small, quality).data);
      if (encoded.length >= 3 && encoded.length <= ceiling && isJpeg(encoded)) return encoded;
    }
  } catch { return undefined; }
  return undefined;
}
