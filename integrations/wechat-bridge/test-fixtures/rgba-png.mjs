/** A real PNG from raw RGBA bytes, for the outbound-picture witness tests. No dependency:
 *  zlib is in node, and the 4-byte CRC table is built here. */
import { deflateSync } from 'node:zlib';

const table = Uint32Array.from({ length: 256 }, (_, n) => {
  let value = n;
  for (let bit = 0; bit < 8; bit++) value = value & 1 ? 0xedb88320 ^ (value >>> 1) : value >>> 1;
  return value >>> 0;
});
const crc32 = bytes => {
  let value = 0xffffffff;
  for (const byte of bytes) value = table[(value ^ byte) & 255] ^ (value >>> 8);
  return (value ^ 0xffffffff) >>> 0;
};
const chunk = (type, data) => {
  const head = Buffer.alloc(8);
  head.writeUInt32BE(data.length, 0);
  head.write(type, 4, 'latin1');
  const tail = Buffer.alloc(4);
  tail.writeUInt32BE(crc32(Buffer.concat([head.subarray(4), data])), 0);
  return Buffer.concat([head, data, tail]);
};

/** `pixels` is width * height * 4 bytes (RGBA, 8-bit), one filter-0 scanline per row. */
export function rgbaPng(pixels, width, height) {
  if (pixels.length !== width * height * 4) throw new Error('not enough pixels');
  const lines = Buffer.alloc(height * (1 + width * 4));
  for (let y = 0; y < height; y++) {
    lines[y * (1 + width * 4)] = 0;
    pixels.copy ? pixels.copy(lines, y * (1 + width * 4) + 1, y * width * 4, (y + 1) * width * 4)
      : lines.set(pixels.subarray(y * width * 4, (y + 1) * width * 4), y * (1 + width * 4) + 1);
  }
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0); ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8; ihdr[9] = 6; ihdr[10] = 0; ihdr[11] = 0; ihdr[12] = 0;
  return Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr), chunk('IDAT', deflateSync(lines)), chunk('IEND', Buffer.alloc(0))]);
}
