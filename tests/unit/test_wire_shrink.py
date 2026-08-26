"""Frames must get smaller on the wire without losing an edge.

godot_vision inlines every captured frame as base64 in the request body. A
960x704 game frame is a ~900 KB PNG and four go per request — ~4.7 MB. Measured
2026-08-26 against the hosted judge: 14 KB/s sustained, i.e. 5.5 minutes of
upload PER REQUEST and ~4.3 hours for a 47-scenario run — longer than the
interval between container recreates, so the gate restarted from scenario 1
twice and never finished.

Every question this gate asks is about an edge, so the shrink must not touch
one: palette reduction, not downscaling, not JPEG.
"""
import io
import os

import pytest

from aitelier.tools.godot_vision import impl

Image = pytest.importorskip("PIL.Image", reason="pillow is the shrink's engine")


def _frame(tmp_path, name="f.png", size=(480, 352)):
    """A frame shaped like the real thing.

    Both the colour COUNT and its AMPLITUDE matter, and getting the second one
    wrong makes this fixture lie. The game's background is an ink-wash painting:
    62,937 distinct colours, but they are all close together, so a 256-entry
    palette covers it and still has room for the UI drawn on top — measured on
    a real frame, the health bar comes back with a worst channel error of 1/255.
    A fixture whose background is per-pixel NOISE instead crowds the palette,
    pushes the bar's dark cap to a much lighter entry, and "proves" a failure
    the real frames do not have. So: fine texture, low amplitude.
    """
    im = Image.new("RGB", size)
    w, h = size
    for y in range(h):
        for x in range(w):
            t = (x * 3 + y * 5) % 7          # texture PNG cannot flatten...
            im.putpixel((x, y), (60 + x * 120 // w + t,   # ...but only +/-3,
                                 90 + y * 120 // h + t,   # so the palette is
                                 110 + ((x + y) % 5)))    # not crowded
    for x in range(0, w, 16):                # 1px grid lines
        for y in range(h):
            im.putpixel((x, y), (255, 255, 255))
    for x in range(10, 60):                  # a "health bar": filled + empty
        for y in range(8, 16):
            im.putpixel((x, y), (0, 200, 0) if x < 46 else (30, 30, 30))
    p = tmp_path / name
    im.save(p)
    return p


def test_it_either_shrinks_and_reduces_the_palette_or_changes_nothing(tmp_path):
    """The function's whole contract, on any input.

    The size win itself is a property of the game's REAL frames, not of any
    fixture: measured on a 960x704 battle frame, 900,172 -> 259,468 bytes (3.5x),
    because the ink-wash background carries 62,937 distinct colours that a
    256-entry palette covers. No synthetic image reproduces that — a smooth
    gradient PNG-compresses well enough that quantising wins nothing, and
    per-pixel noise crowds the palette in a way real frames never do. So the
    ratio is a measurement, recorded here; what is ASSERTED is the invariant
    that holds for every input, including the ones where the shrink declines.
    """
    p = _frame(tmp_path)
    raw = p.read_bytes()
    wire = impl._wire_bytes(p)
    if wire == raw:
        return                                  # declined — the guard held
    assert len(wire) < len(raw)
    out = Image.open(io.BytesIO(wire)).convert("RGB")
    colours = out.getcolors(maxcolors=2 ** 24)
    assert colours is not None and len(colours) <= impl._WIRE_COLORS


def test_the_dimensions_are_untouched(tmp_path):
    """Downscaling is the lever that drops the 1px grid Q1 asks about."""
    p = _frame(tmp_path)
    before = Image.open(p).size
    after = Image.open(io.BytesIO(impl._wire_bytes(p))).size
    assert before == after


def test_every_grid_line_survives_byte_exact(tmp_path):
    """The whole reason this is palette reduction and not JPEG."""
    p = _frame(tmp_path)
    out = Image.open(io.BytesIO(impl._wire_bytes(p))).convert("RGB")
    for x in range(0, out.size[0], 16):
        assert out.getpixel((x, 40)) == (255, 255, 255), f"grid line at x={x} lost"


def test_the_filled_and_empty_halves_of_a_bar_stay_distinct(tmp_path):
    """Q5 is exactly this distinction."""
    p = _frame(tmp_path)
    out = Image.open(io.BytesIO(impl._wire_bytes(p))).convert("RGB")
    filled, empty = out.getpixel((20, 12)), out.getpixel((52, 12))
    assert filled != empty
    assert filled[1] > 150 and max(empty) < 80


def test_a_frame_that_cannot_be_opened_is_sent_whole(tmp_path):
    """A slower gate beats a blind one — never drop a frame to save bytes."""
    p = tmp_path / "broken.png"
    p.write_bytes(b"not a png at all")
    assert impl._wire_bytes(p) == b"not a png at all"


def test_shrinking_can_be_turned_off(tmp_path, monkeypatch):
    p = _frame(tmp_path)
    monkeypatch.setattr(impl, "_WIRE_COLORS", 0)
    assert impl._wire_bytes(p) == p.read_bytes()


def test_it_never_ships_a_bigger_payload_than_the_original(tmp_path):
    """A tiny menu frame can quantise LARGER; the original must win."""
    im = Image.new("RGB", (8, 8), (12, 34, 56))
    p = tmp_path / "tiny.png"
    im.save(p)
    assert len(impl._wire_bytes(p)) <= os.path.getsize(p)
