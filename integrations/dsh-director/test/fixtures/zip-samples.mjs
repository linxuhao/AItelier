/** Step 24.2d: a zip built from one of media-types.json's `container_samples` - local headers
 *  with `packed` bytes of filler each, a central directory declaring each entry's sizes and
 *  flags, and an End of Central Directory (or none, a zip64 locator before it, a directory said
 *  to start past it). tests/unit/office_zips.py builds the same bytes for the backend's side. */
import { readFileSync } from 'node:fs';

export const TABLE = JSON.parse(readFileSync(new URL('../../media-types.json', import.meta.url), 'utf8'));
export const SAMPLES = TABLE.container_samples;

export function zipFrom({ entries, filler = 0, eocd }) {
  const all = [...entries, ...Array.from({ length: filler }, (_, i) => [`f/${i}.xml`, 10, 10])];
  const locals = [], central = [];
  let offset = 0;
  for (const [name, plain, packed, flags = 0] of all) {
    const nameBytes = Buffer.from(name, 'utf8');
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0); local.writeUInt16LE(20, 4); local.writeUInt16LE(flags, 6); local.writeUInt16LE(8, 8);
    local.writeUInt32LE(packed, 18); local.writeUInt32LE(plain, 22); local.writeUInt16LE(nameBytes.length, 26);
    locals.push(local, nameBytes, Buffer.alloc(packed));
    const record = Buffer.alloc(46);
    record.writeUInt32LE(0x02014b50, 0); record.writeUInt16LE(20, 4); record.writeUInt16LE(20, 6); record.writeUInt16LE(flags, 8);
    record.writeUInt16LE(8, 10); record.writeUInt32LE(packed, 20); record.writeUInt32LE(plain, 24);
    record.writeUInt16LE(nameBytes.length, 28); record.writeUInt32LE(offset, 42);
    central.push(record, nameBytes);
    offset += 30 + nameBytes.length + packed;
  }
  const directory = Buffer.concat(central);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0); end.writeUInt16LE(all.length, 8); end.writeUInt16LE(all.length, 10);
  end.writeUInt32LE(directory.length, 12); end.writeUInt32LE(eocd === 'past_end' ? offset + 100 : offset, 16);
  const tail = eocd === 'missing' ? [] : eocd === 'zip64' ? [Buffer.from('504b0607' + '00'.repeat(16), 'hex'), end] : [end];
  return Buffer.concat([...locals, directory, ...tail]);
}

/** A small well-formed sample of each OOXML type, by extension. */
export const ooxml = ext => zipFrom(SAMPLES.find(sample => sample.about === { docx: 'a docx', xlsx: 'an xlsx', pptx: 'a pptx' }[ext]));
