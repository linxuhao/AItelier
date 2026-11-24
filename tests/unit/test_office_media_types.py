"""Step 24.2d (design-steward 7.8.12c "六", "十六"): the backend's half of the office types - the
zip-directory judgement (core/media_types.py ooxml_type) on the table's container_samples, the
same bytes and the same answers as the host's lib/media.mjs ooxmlType, and the first bytes alone
never naming an office type."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from core import media_types
from tests.unit.office_zips import ooxml, samples, zip_from

REPO = Path(__file__).resolve().parents[2]
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.mark.parametrize("sample", samples(), ids=lambda s: s["about"])
def test_every_container_sample_answers_its_mime(sample):
    assert (media_types.ooxml_type(zip_from(sample)) or {}).get("mime") == sample["mime"]


def test_the_first_bytes_never_name_an_office_type_and_identify_reads_the_whole_zip():
    for sample in media_types.table()["samples"]:
        assert (media_types.sniff(bytes.fromhex(sample["hex"])) or {}).get("mime") == sample["mime"], sample
    assert media_types.sniff(ooxml("docx")[:64]) is None
    assert media_types.identify(ooxml("docx"))["mime"] == DOCX
    assert media_types.identify(ooxml("xlsx"))["ext"] == "xlsx"
    assert media_types.identify(ooxml("pptx"))["ext"] == "pptx"
    assert media_types.identify(bytes.fromhex("d0cf11e0a1b11ae1") + b"\0" * 600) is None
    assert media_types.identify(b"%PDF-1.7\n")["mime"] == "application/pdf"
    assert media_types.identify(bytes.fromhex("504b0304") + b"\xff" * 200) is None
    assert media_types.identify(ooxml("docx") + b"trailing") is None
    assert media_types.ext_of(DOCX) == "docx"
    assert media_types.made_up_name("file", "m_0123456789abcdef", DOCX) == "file-01234567.docx"


def test_the_judgement_never_unzips():
    source = (REPO / "core/media_types.py").read_text()
    assert "zipfile" not in source and "zlib" not in source


def test_the_host_builds_the_same_zips_and_answers_the_same():
    """Both sides are tested on the very same bytes: the JS fixture's zips hash like these, and
    lib/media.mjs ooxmlType answers what ooxml_type answers on each."""
    script = ("Promise.all([import('./integrations/dsh-director/test/fixtures/zip-samples.mjs'), import('./integrations/dsh-director/lib/media.mjs')])"
              ".then(([z, m]) => console.log(JSON.stringify(z.SAMPLES.map(s => { const b = z.zipFrom(s);"
              " return [require('crypto').createHash('sha256').update(b).digest('hex'), m.ooxmlType(b)?.mime ?? null]; }))))")
    node = subprocess.run(["node", "-e", script], cwd=REPO, capture_output=True, text=True, timeout=60)
    if node.returncode != 0 and "not found" in (node.stderr or ""):
        pytest.skip("node is not installed")
    assert node.returncode == 0, node.stderr
    ours = [[hashlib.sha256(zip_from(s)).hexdigest(), (media_types.ooxml_type(zip_from(s)) or {}).get("mime")] for s in samples()]
    assert json.loads(node.stdout) == ours


def test_the_limits_the_judgement_reads_are_the_tables():
    limits = media_types.limits()
    assert (limits["office_zip_entries"], limits["office_unzipped_bytes"], limits["office_zip_ratio"]) == (2000, 100 * 1024 * 1024, 200)
    assert limits["kept_mimes"][-1] == "application/pdf"
    assert not any("officedocument" in mime for mime in limits["kept_mimes"])
