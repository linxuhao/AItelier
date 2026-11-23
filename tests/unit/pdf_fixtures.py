"""PDF fixtures for the PDF reader tests, built from bytes (stdlib, plus PIL for images).

Nothing here is checked in as a binary: every fixture is written by code, so a
reviewer can read exactly what each PDF holds. The generators:

  * latin_pdf: Type1 Helvetica text pages (one string per line);
  * cjk_pdf: a Type0 / Identity-H font with a ToUnicode CMap and no embedded
    font program, so the text layer is CJK while the glyphs are unavailable;
  * image_pdf: a page that is only a picture, saved by PIL (no text layer);
  * noise_pdf: an image-only page of random grey noise, which JPEG cannot squeeze;
  * encrypted_pdf: the Standard security handler, RC4 40-bit (V1/R2), with a user
    password, with an owner password only, or with an unknown /Filter name;
  * truncated() and GARBAGE: bytes that are not a readable PDF.
"""
from __future__ import annotations

import hashlib
import io
import random
import struct

# The 32-byte padding string of the Standard security handler (PDF 1.7, 7.6.3.3).
PAD = bytes.fromhex("28BF4E5E4E758A4164004E56FFFA01082E2E00B6D0683E802F0CA9FE6453697A")
FILE_ID = bytes.fromhex("0123456789abcdef0123456789abcdef")
GARBAGE = b"this is not a pdf at all\n" * 40

LATIN_LINES = ["Hello steward PDF reader", "Second line 12345", "Third line: totals 4721"]
CJK_LINES = ["关于二〇二六年十二月物业费调整的通知", "各位业主：自2026年12月1日起，物业费调整为2.68元。"]


def _rc4(key: bytes, data: bytes) -> bytes:
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) % 256
        s[i], s[j] = s[j], s[i]
    out = bytearray()
    i = j = 0
    for byte in data:
        i = (i + 1) % 256
        j = (j + s[i]) % 256
        s[i], s[j] = s[j], s[i]
        out.append(byte ^ s[(s[i] + s[j]) % 256])
    return bytes(out)


def _pad(password: bytes) -> bytes:
    return (password + PAD)[:32]


def _pdf_string(text: str) -> bytes:
    return b"(" + text.encode("latin-1").replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


def build_pdf(objects: list[tuple[bytes, bytes | None]], *, encrypt: dict | None = None) -> bytes:
    """Serialise numbered objects (1-based, object 1 is the catalog) with an xref table.

    Each object is (dictionary bytes, stream bytes or None). With `encrypt`
    ({"dict": bytes, "key": bytes}), every stream is RC4-encrypted with its object
    key and the encryption dictionary is appended as the last object.
    """
    objects = list(objects)
    encrypt_num = None
    if encrypt is not None:
        objects.append((encrypt["dict"], None))
        encrypt_num = len(objects)
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for num, (head, stream) in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % num
        if stream is None:
            out += head
        else:
            if encrypt is not None and num != encrypt_num:
                okey = hashlib.md5(encrypt["key"] + struct.pack("<I", num)[:3] + b"\x00\x00").digest()
                stream = _rc4(okey[: min(len(encrypt["key"]) + 5, 16)], stream)
            out += head[:-2] + b" /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
        out += b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    trailer = b"<< /Size %d /Root 1 0 R /ID [<%s> <%s>]" % (len(objects) + 1, FILE_ID.hex().encode(), FILE_ID.hex().encode())
    if encrypt_num is not None:
        trailer += b" /Encrypt %d 0 R" % encrypt_num
    out += b"trailer\n" + trailer + b" >>\nstartxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


def _pages(contents: list[bytes], font: bytes, media: tuple[float, float], extra: list[tuple[bytes, bytes | None]] = ()):
    """Catalog, page tree, one page per content stream, then the font and extras.

    Object layout: 1 catalog, 2 pages, 3 font, 4.. extras, then page/content pairs.
    """
    extra = list(extra)
    first_page = 4 + len(extra)
    kids = b" ".join(b"%d 0 R" % (first_page + 2 * i) for i in range(len(contents)))
    objects = [
        (b"<< /Type /Catalog /Pages 2 0 R >>", None),
        (b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(contents)), None),
        (font, None),
        *extra,
    ]
    box = b"[0 0 %s %s]" % (repr(media[0]).encode(), repr(media[1]).encode())
    for i, content in enumerate(contents):
        page_num = first_page + 2 * i
        objects.append((b"<< /Type /Page /Parent 2 0 R /MediaBox %s /Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % (box, page_num + 1), None))
        objects.append((b"<< >>", content))
    return objects


def latin_content(lines: list[str], size: int = 12, top: float = 720) -> bytes:
    body = b"BT /F1 %d Tf %d TL 72 %s Td " % (size, size * 2, repr(top).encode())
    body += b" T* ".join(_pdf_string(line) + b" Tj" for line in lines)
    return body + b" ET\n"


HELVETICA = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"


def latin_objects(pages: list[list[str]], media=(612, 792)):
    return _pages([latin_content(lines, top=media[1] - 72) for lines in pages], HELVETICA, media)


def latin_pdf(pages: list[list[str]] | None = None, media=(612, 792)) -> bytes:
    return build_pdf(latin_objects(pages or [LATIN_LINES], media))


def cjk_pdf(pages: list[list[str]] | None = None) -> bytes:
    """CJK text through a Type0 Identity-H font: CIDs are assigned per distinct
    character, the ToUnicode CMap maps them back, and no font program is embedded."""
    pages = pages or [CJK_LINES]
    chars = sorted({ch for lines in pages for line in lines for ch in line})
    cid = {ch: i + 1 for i, ch in enumerate(chars)}
    entries = [b"<%04x> <%s>" % (cid[ch], ch.encode("utf-16-be").hex().encode()) for ch in chars]
    blocks = b""
    for i in range(0, len(entries), 100):
        chunk = entries[i:i + 100]
        blocks += b"%d beginbfchar\n" % len(chunk) + b"\n".join(chunk) + b"\nendbfchar\n"
    cmap = (b"/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
            b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
            b"/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
            b"1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
            + blocks + b"endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n")
    font = b"<< /Type /Font /Subtype /Type0 /BaseFont /SimSun /Encoding /Identity-H /DescendantFonts [4 0 R] /ToUnicode 6 0 R >>"
    extra = [
        (b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /SimSun /CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >>"
         b" /FontDescriptor 5 0 R /DW 1000 /CIDToGIDMap /Identity >>", None),
        (b"<< /Type /FontDescriptor /FontName /SimSun /Flags 4 /FontBBox [0 -140 1000 880] /ItalicAngle 0"
         b" /Ascent 880 /Descent -120 /CapHeight 700 /StemV 80 >>", None),
        (b"<< >>", cmap),
    ]
    contents = []
    for lines in pages:
        body = b"BT /F1 10 Tf 14 TL 36 800 Td "
        body += b" T* ".join(b"<" + b"".join(b"%04x" % cid[ch] for ch in line) + b"> Tj" for line in lines)
        contents.append(body + b" ET\n")
    return build_pdf(_pages(contents, font, (595, 842), extra))


def image_pdf(image, resolution: float = 150.0) -> bytes:
    """A PDF whose only page is `image` (a PIL image), as PIL itself saves it."""
    buf = io.BytesIO()
    image.save(buf, "PDF", resolution=resolution)
    return buf.getvalue()


def noise_pdf(size=(1100, 1540), seed: int = 4721) -> bytes:
    """An image-only page of random grey noise: JPEG stays large at any quality."""
    from PIL import Image

    rnd = random.Random(seed)
    data = bytes(rnd.getrandbits(8) for _ in range(size[0] * size[1]))
    return image_pdf(Image.frombytes("L", size, data), resolution=150.0)


def encrypted_pdf(user: str, owner: str, *, filter_name: str = "Standard", lines=None) -> bytes:
    """A Latin page under the Standard handler, RC4 40-bit (V1, R2).

    With user="" the file opens without a password (owner password only).
    `filter_name` other than "Standard" names a security handler PDFium does not have.
    """
    perms = -44
    upw, opw = user.encode("latin-1"), (owner or user).encode("latin-1")
    o_value = _rc4(hashlib.md5(_pad(opw)).digest()[:5], _pad(upw))
    key = hashlib.md5(_pad(upw) + o_value + struct.pack("<i", perms) + FILE_ID).digest()[:5]
    u_value = _rc4(key, PAD)
    enc = (b"<< /Filter /%s /V 1 /R 2 /Length 40 /O <%s> /U <%s> /P %d >>"
           % (filter_name.encode(), o_value.hex().encode(), u_value.hex().encode(), perms))
    return build_pdf(latin_objects([lines or LATIN_LINES]), encrypt={"dict": enc, "key": key})


def truncated(data: bytes, keep: int = 40) -> bytes:
    """The first `keep` bytes: a header and the start of the first object, no catalog."""
    return data[:keep]
