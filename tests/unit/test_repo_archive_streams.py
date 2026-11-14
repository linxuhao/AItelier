"""`repo_archive` streams its zip instead of building it in an in-memory buffer.

The defect (issue iss-2f4a4343e3254784): the whole working tree was deflated into
an io.BytesIO before the first byte was sent, so one request held a buffer that
grew with the repository, and a slow reader pinned it for as long as it liked.

Two properties must survive the fix, and this file measures both on the same
trees in the same run:

1. The archive is the same as before: the same member names in the same order,
   and the same uncompressed bytes per member (compared by sha256). The
   container's own bytes are not compared; a different deflate stream is the
   same archive.
2. The memory a request holds does not grow with the tree. Peak allocations are
   measured with tracemalloc for a small and a 10x-larger tree. The pre-change
   implementation is measured the same way as a control: if the control did not
   blow past the bound on the large tree, the bound would prove nothing.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import os
import tracemalloc
import zipfile
from dataclasses import dataclass
from pathlib import Path

import api.project_routers as pr

# A number the fix must stay under on both trees. It is deliberately not read
# from the implementation's constants: it sits above the spool threshold with
# room for the copy SpooledTemporaryFile makes when it rolls over to disk.
PEAK_BOUND_BYTES = 4 * 1024 * 1024

N_BLOBS = 5
SMALL_BLOB_BYTES = 300 * 1024
# The fixed-size small files make the total ratio land a little under the blob
# ratio, so the blobs are scaled to 11x to put the whole tree clearly past 10x.
BIG_BLOB_BYTES = 11 * SMALL_BLOB_BYTES


class _Db:
    def get_project(self, project_id):
        return {"project_id": project_id, "owner_email": "cli@local"}


class _Ws:
    def __init__(self, base: Path):
        self._base = base

    def get_code_path(self, project_id, run_id=None):
        return self._base


def _make_tree(root: Path, blob_bytes: int) -> Path:
    """A small tree with nested dirs, mixed-case names, and a `.git` to exclude.

    Blobs are random so they do not deflate; the archive size then tracks the
    content size, which is what the measurement needs to vary."""
    files = {
        "README.md": b"# project\n",
        "Upper.txt": b"mixed case sorts differently\n",
        "src/a.gd": b"extends Node\n",
        "src/b/deep.txt": b"nested\n",
        ".git/config": b"[core]\n\trepositoryformatversion = 0\n",
        ".git/objects/pack/p.pack": b"PACK-not-for-the-archive",
    }
    for i in range(N_BLOBS):
        files[f"assets/blob_{i}.bin"] = os.urandom(blob_bytes)
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def _content_bytes(tree: Path) -> int:
    """Uncompressed bytes of the members the archive should contain."""
    return sum(p.stat().st_size for p in tree.rglob("*")
               if p.is_file() and ".git" not in p.relative_to(tree).parts)


def _reference_zip(code_path: Path) -> io.BytesIO:
    """The pre-change body of `repo_archive`, kept here as the oracle.

    It is what the route did before the fix: deflate the whole tree into a
    BytesIO and hand that buffer over. Returned unread, as the old route held it.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in sorted(code_path.rglob("*")):
            if ".git" in item.parts:
                continue
            if item.is_file():
                zf.write(item, arcname=str(item.relative_to(code_path)))
    buf.seek(0)
    return buf


def _serve_streaming(tree: Path, dest: Path):
    """Run the real route and drain its body to `dest` on disk.

    Writing to disk keeps the body out of memory, so the measured peak is the
    implementation's own and not the test collecting the response."""
    response = pr.repo_archive("p1", user=None, db=_Db(), ws=_Ws(tree), _=None)

    async def drain():
        with open(dest, "wb") as fh:
            async for chunk in response.body_iterator:
                fh.write(chunk)

    asyncio.run(drain())
    return response


def _measure_peak(fn):
    """Peak bytes allocated above the level at entry while `fn` runs."""
    started_here = not tracemalloc.is_tracing()
    if started_here:
        tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        base, _ = tracemalloc.get_traced_memory()
        result = fn()
        _, peak = tracemalloc.get_traced_memory()
        return max(0, peak - base), result
    finally:
        if started_here:
            tracemalloc.stop()


@dataclass
class _Run:
    content: int
    new_peak: int
    ref_peak: int


def test_streamed_archive_matches_pre_change_and_peak_is_bounded(tmp_path, capsys):
    runs: dict[str, _Run] = {}

    for label, blob_bytes in (("small", SMALL_BLOB_BYTES), ("big", BIG_BLOB_BYTES)):
        tree = _make_tree(tmp_path / label, blob_bytes)
        dest = tmp_path / f"{label}.zip"

        new_peak, response = _measure_peak(lambda: _serve_streaming(tree, dest))
        ref_peak, ref_buf = _measure_peak(lambda: _reference_zip(tree))

        # Same response shape as before: zip media type and the same filename.
        assert response.media_type == "application/zip"
        assert response.headers["content-disposition"] == 'attachment; filename="p1.zip"'

        # (1) Same members, same order, same uncompressed bytes.
        with zipfile.ZipFile(ref_buf) as ref, zipfile.ZipFile(dest) as new:
            ref_names = [i.filename for i in ref.infolist()]
            new_names = [i.filename for i in new.infolist()]
            assert ref_names, "reference archive is empty; the trees are wrong"
            assert not any(n.startswith(".git") for n in ref_names)
            assert new_names == ref_names, "member names or order differ"
            for name in ref_names:
                new_sha = hashlib.sha256(new.read(name)).hexdigest()
                ref_sha = hashlib.sha256(ref.read(name)).hexdigest()
                assert new_sha == ref_sha, f"member content differs: {name}"

        runs[label] = _Run(_content_bytes(tree), new_peak, ref_peak)
        # capsys.disabled() so the numbers reach the terminal under `-q`, not
        # only the failure report.
        with capsys.disabled():
            print(f"\n[repo_archive] {label}: content={runs[label].content} bytes, "
                  f"members={len(ref_names)}, "
                  f"streamed peak={new_peak} bytes, pre-change peak={ref_peak} bytes")

    small, big = runs["small"], runs["big"]

    # The two trees really do differ by at least 10x.
    assert big.content >= 10 * small.content

    # (2) Both peaks sit under one constant bound, and the large tree's peak is
    # not a multiple of the small one's: the 10x content does not reach the RAM.
    assert small.new_peak <= PEAK_BOUND_BYTES, small
    assert big.new_peak <= PEAK_BOUND_BYTES, big
    assert big.new_peak <= 2 * small.new_peak, (small, big)

    # Control: the pre-change implementation does scale and does exceed the
    # bound on the large tree, so the measurement can tell the two apart.
    assert big.ref_peak > PEAK_BOUND_BYTES, big
    assert big.ref_peak >= 5 * small.ref_peak, (small, big)
    assert big.new_peak * 4 < big.ref_peak, (small, big)
