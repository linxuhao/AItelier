"""The steward's PDF reader child, integrations/dsh-director/pdf_read.py (design 7.8.12b §6, §7, §16).

Every run here starts the script the way the host will: `python -I pdf_read.py ...`
with only PATH, LC_ALL and HOME in the environment, the input and output in a
private temporary directory, and stdout taken as the one JSON line it prints.
The fixtures are written by tests/unit/pdf_fixtures.py from bytes.

pypdfium2 is not in the image until it is rebuilt with requirements-pdf.txt, so the
module skips (loudly) when it cannot be imported. Run it in a throwaway venv:
  python -m venv --system-site-packages /tmp/pv
  /tmp/pv/bin/pip install --no-index --find-links <wheel dir> --require-hashes \
      --only-binary=:all: --no-deps -r requirements-pdf.txt
  /tmp/pv/bin/python -m pytest tests/unit/test_pdf_read.py
"""
from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

try:
    import pypdfium2  # noqa: F401
except ImportError:  # pragma: no cover - depends on the image
    pytestmark = pytest.mark.skip(
        reason="SKIPPED LOUDLY: pypdfium2 is not installed in this interpreter. The image gets it from "
               "requirements-pdf.txt on its next rebuild; until then run this file in a venv with the wheel.")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pdf_fixtures as F  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "integrations" / "dsh-director" / "pdf_read.py"
SANDBOXES = {"landlock+seccomp", "landlock_fs+seccomp", "seccomp", "landlock", "landlock_fs", "none"}


def _env(home):
    return {"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8", "HOME": str(home)}


def _load_module():
    spec = importlib.util.spec_from_file_location("pdf_read_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.load_libraries()
    return module


class Run:
    def __init__(self, proc, out_dir):
        self.rc = proc.returncode
        self.stdout = proc.stdout
        self.stderr = proc.stderr
        self.out_dir = out_dir
        assert proc.stdout.count(b"\n") == 1 and proc.stdout.endswith(b"\n"), proc.stdout[:400] + proc.stderr[-2000:]
        self.result = json.loads(proc.stdout)

    @property
    def files(self):
        return sorted(os.listdir(self.out_dir))

    def image(self, n):
        from PIL import Image
        img = Image.open(io.BytesIO((self.out_dir / f"p{n}.jpg").read_bytes()))
        img.load()
        return img


@pytest.fixture
def read(tmp_path):
    counter = iter(range(1000))

    def run(data: bytes, *args: str) -> Run:
        i = next(counter)
        work = tmp_path / f"w{i}"
        out = work / "out"
        out.mkdir(parents=True)
        source = work / "in.pdf"
        source.write_bytes(data)
        proc = subprocess.run([sys.executable, "-I", str(SCRIPT), str(source), str(out), *args],
                              capture_output=True, env=_env(work), cwd=work, timeout=60)
        run_ = Run(proc, out)
        assert str(work) not in proc.stdout.decode("utf-8"), "a path leaked into stdout"
        _assert_no_path_key(run_.result)
        return run_

    return run


def _assert_no_path_key(value):
    if isinstance(value, dict):
        for key, item in value.items():
            assert "path" not in key.lower(), f"stdout carries a path field: {key}"
            _assert_no_path_key(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_path_key(item)


def _quant_tables(quality, mode="L"):
    from PIL import Image
    buf = io.BytesIO()
    Image.new(mode, (64, 64), 128).save(buf, "JPEG", quality=quality)
    return Image.open(io.BytesIO(buf.getvalue())).quantization


# -- text layer --------------------------------------------------------------

def test_latin_text_layer_is_verbatim_and_the_page_image_is_rgb(read):
    run = read(F.latin_pdf())
    assert run.rc == 0
    assert run.result["page_count"] == 1 and run.result["sandbox"] in SANDBOXES
    [page] = run.result["pages"]
    assert page["text"] == "\r\n".join(F.LATIN_LINES)
    assert page["chars"] == len(page["text"]) and page["truncated"] is False
    assert page["no_text_layer"] is False and page["image"] is True
    assert run.files == ["p1.jpg"]
    img = run.image(1)
    assert img.format == "JPEG" and img.mode == "RGB"
    assert (img.width, img.height) == (page["width"], page["height"])


def test_cjk_text_layer_through_tounicode_is_verbatim(read):
    run = read(F.cjk_pdf())
    assert run.rc == 0
    [page] = run.result["pages"]
    assert page["text"] == "\r\n".join(F.CJK_LINES)
    assert page["no_text_layer"] is False
    assert "关于" in run.stdout.decode("utf-8"), "stdout must carry CJK unescaped (ensure_ascii=False)"


def test_an_image_only_page_has_no_text_layer_and_a_grayscale_jpeg(read):
    from PIL import Image
    run = read(F.image_pdf(Image.new("RGB", (600, 800), (200, 30, 30))))
    assert run.rc == 0
    [page] = run.result["pages"]
    assert page["no_text_layer"] is True
    assert page["text"] == "" and page["chars"] == 0 and page["truncated"] is False
    assert page["image"] is True
    assert run.image(1).mode == "L"


def test_a_whitespace_only_text_layer_counts_as_none(read):
    run = read(F.latin_pdf([["   ", " ", "    "]]))
    [page] = run.result["pages"]
    assert page["no_text_layer"] is True and page["text"] == ""
    assert run.image(1).mode == "L"


def test_text_only_writes_no_file(read):
    run = read(F.latin_pdf([["one"], ["two"]]), "--images", "0")
    assert run.rc == 0
    assert [p["text"] for p in run.result["pages"]] == ["one", "two"]
    assert all(p["image"] is False and "width" not in p for p in run.result["pages"])
    assert run.files == []


def test_files_are_named_by_page_number_only(read):
    run = read(F.latin_pdf([["a"], ["b"], ["c"]]), "--pages", "2-3")
    assert [p["n"] for p in run.result["pages"]] == [2, 3]
    assert run.files == ["p2.jpg", "p3.jpg"]


# -- refusals ----------------------------------------------------------------

def test_a_user_password_is_encrypted(read):
    data = F.encrypted_pdf("user123", "owner456")
    # The fixture is a real encrypted file, not an unreadable one: with the password it reads.
    doc = pypdfium2.PdfDocument(data, password="user123")
    assert doc[0].get_textpage().get_text_range() == "\r\n".join(F.LATIN_LINES)
    doc.close()
    run = read(data)
    assert run.rc != 0
    assert run.result["error"] == "encrypted" and run.result["sandbox"] in SANDBOXES
    assert run.files == []


def test_an_owner_password_alone_still_reads(read):
    run = read(F.encrypted_pdf("", "owner456"))
    assert run.rc == 0
    assert run.result["pages"][0]["text"] == "\r\n".join(F.LATIN_LINES)


def test_an_unknown_security_handler_is_unsupported_security(read):
    data = F.encrypted_pdf("", "owner456", filter_name="AcmeSecure")
    # The design assumes PDFium says FPDF_ERR_SECURITY for an unknown /Filter; check it does.
    with pytest.raises(pypdfium2.PdfiumError) as caught:
        pypdfium2.PdfDocument(data)
    assert caught.value.err_code == pypdfium2.raw.FPDF_ERR_SECURITY
    run = read(data)
    assert run.rc != 0 and run.result["error"] == "unsupported_security"


def test_only_the_password_code_maps_to_encrypted():
    module = _load_module()
    raw = pypdfium2.raw
    codes = {raw.FPDF_ERR_SUCCESS: "broken", raw.FPDF_ERR_UNKNOWN: "broken", raw.FPDF_ERR_FILE: "broken",
             raw.FPDF_ERR_FORMAT: "broken", raw.FPDF_ERR_PASSWORD: "encrypted",
             raw.FPDF_ERR_SECURITY: "unsupported_security", raw.FPDF_ERR_PAGE: "broken", None: "broken", 99: "broken"}
    for code, expected in codes.items():
        assert module.open_error(pypdfium2.PdfiumError("x", err_code=code)) == expected, code
    assert module.open_error(ValueError("not pdfium")) == "broken"


@pytest.mark.parametrize("data", [F.truncated(F.latin_pdf()), F.latin_pdf()[:300], F.GARBAGE, b""],
                         ids=["header-only", "cut-at-300", "garbage", "empty"])
def test_truncated_or_garbage_bytes_are_broken(read, data):
    run = read(data)
    assert run.rc != 0 and run.result["error"] == "broken"
    assert run.files == []


def test_a_start_page_past_the_end_is_page_out_of_range_with_the_count(read):
    run = read(F.latin_pdf([["a"], ["b"]]), "--pages", "3-4")
    assert run.rc != 0
    assert run.result["error"] == "page_out_of_range" and run.result["page_count"] == 2
    assert run.files == []


def test_the_end_page_is_clipped_to_the_document(read):
    run = read(F.latin_pdf([["a"], ["b"]]), "--pages", "2-4")
    assert run.rc == 0 and run.result["page_count"] == 2
    assert [p["n"] for p in run.result["pages"]] == [2]


def test_without_pages_the_first_pages_up_to_the_cap_are_read(read):
    doc = F.latin_pdf([[str(i)] for i in range(1, 8)])
    assert [p["n"] for p in read(doc).result["pages"]] == [1, 2, 3, 4]
    assert [p["n"] for p in read(doc, "--images", "0").result["pages"]] == list(range(1, 8))


@pytest.mark.parametrize("spec,images,error", [
    ("x", "1", "invalid_request"), ("0-1", "1", "invalid_request"), ("3-2", "1", "invalid_request"),
    ("1-", "1", "invalid_request"), ("-2", "1", "invalid_request"),
    ("1-5", "1", "too_many_pages"), ("1-31", "0", "too_many_pages"),
])
def test_bad_page_specs_are_refused(read, spec, images, error):
    run = read(F.latin_pdf([["a"]] * 40), "--pages", spec, "--images", images)
    assert run.rc != 0 and run.result["error"] == error


def test_thirty_pages_text_only_is_allowed(read):
    run = read(F.latin_pdf([[f"page {i}"] for i in range(1, 41)]), "--pages", "1-30", "--images", "0")
    assert run.rc == 0 and len(run.result["pages"]) == 30


def test_a_failing_page_records_its_error_and_the_others_go_on(monkeypatch):
    module = _load_module()
    real = module.read_page

    def flaky(pdf, n, *args):
        if n == 2:
            raise RuntimeError("render failed")
        return real(pdf, n, *args)

    monkeypatch.setattr(module, "read_page", flaky)
    opts = module.parse_args(["in.pdf", "out", "--images", "0"])
    result, status = module.read_document(F.latin_pdf([["a"], ["b"], ["c"]]), opts, None, "none")
    assert status == 0
    assert [(p["n"], p.get("error"), p["text"]) for p in result["pages"]] == [(1, None, "a"), (2, "broken", ""), (3, None, "c")]


# -- render caps -------------------------------------------------------------

def test_a_poster_page_is_scaled_into_the_long_side_and_pixel_caps(read):
    run = read(F.latin_pdf(media=(5000, 7000)))
    [page] = run.result["pages"]
    img = run.image(1)
    assert (img.width, img.height) == (page["width"], page["height"])
    assert max(img.size) <= 1540 and img.width * img.height <= 2_500_000
    assert max(img.size) >= 1500, "the long side should land on the cap, not far under it"


def test_the_pixel_cap_binds_when_it_is_the_smallest(read):
    run = read(F.latin_pdf(media=(3000, 3000)), "--max-pixels", "1000000")
    img = run.image(1)
    assert img.width * img.height <= 1_000_000 and img.width * img.height >= 950_000


def test_a_small_page_renders_at_150_dpi(read):
    run = read(F.latin_pdf([["small"]], media=(288, 144)))
    assert run.image(1).size == (600, 300)


def test_arguments_above_the_ceilings_are_clamped(read):
    run = read(F.latin_pdf([["x" * 50]], media=(5000, 7000)), "--dpi", "600", "--long-side", "9000",
               "--max-pixels", "90000000", "--chars-page", "99999", "--chars-call", "99999")
    img = run.image(1)
    assert max(img.size) <= 1540 and img.width * img.height <= 2_500_000
    small = read(F.latin_pdf([["small"]], media=(288, 144)), "--dpi", "600")
    assert small.image(1).size == (600, 300)


def test_render_scale_keeps_the_rounded_up_size_inside_the_caps():
    import math
    module = _load_module()
    for w in range(100, 6000, 7):
        for h in (w * 1.414, w / 1.414, w, 842.0):
            s = module.render_scale(w, h, 150, 1540, 2_500_000)
            pw, ph = math.ceil(w * s), math.ceil(h * s)
            assert max(pw, ph) <= 1540 and pw * ph <= 2_500_000, (w, h, s)
    s = module.render_scale(3000, 3000, 150, 1540, 1_000_000)
    assert math.ceil(3000 * s) ** 2 <= 1_000_000


def test_a_noisy_page_over_512kb_drops_to_q70_and_then_to_1280(read):
    run = read(F.noise_pdf())
    [page] = run.result["pages"]
    img = run.image(1)
    assert max(img.size) == 1280
    assert img.mode == "L"
    assert img.quantization == _quant_tables(70)


def test_jpeg_quality_steps_against_the_limit():
    from PIL import Image
    module = _load_module()
    rnd = __import__("random").Random(7)
    img = Image.frombytes("L", (1500, 1500), rnd.randbytes(1500 * 1500))
    sizes = {}
    for q in (85, 70):
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=q)
        sizes[q] = buf.tell()
    assert sizes[85] > sizes[70]

    def quality(data):
        return Image.open(io.BytesIO(data)).quantization

    data, size = module.encode_jpeg(img, limit=sizes[85])
    assert size == (1500, 1500) and quality(data) == _quant_tables(85)
    data, size = module.encode_jpeg(img, limit=(sizes[85] + sizes[70]) // 2)
    assert size == (1500, 1500) and quality(data) == _quant_tables(70)
    data, size = module.encode_jpeg(img, limit=1000)
    assert size == (1280, 1280) and quality(data) == _quant_tables(70)
    small = img.resize((900, 600))
    data, size = module.encode_jpeg(small, limit=1000)
    assert size == (900, 600), "an image already under the fallback size is not enlarged"


# -- text caps ---------------------------------------------------------------

def _cjk_pages(n_pages, chars_per_page):
    alphabet = [chr(0x4E00 + i) for i in range(1500)]
    pages = []
    for p in range(n_pages):
        text = "".join(alphabet[(p * 7 + i) % len(alphabet)] for i in range(chars_per_page))
        pages.append([text[i:i + 50] for i in range(0, len(text), 50)])
    return pages


def test_per_page_and_per_call_caps_set_truncated(read):
    source = _cjk_pages(3, 5000)
    run = read(F.cjk_pdf(source), "--pages", "1-3")
    assert run.rc == 0
    pages = run.result["pages"]
    assert [len(p["text"]) for p in pages] == [4000, 4000, 0]
    assert all(p["truncated"] for p in pages)
    assert all(p["chars"] > 5000 for p in pages)
    assert sum(len(p["text"]) for p in pages) == 8000
    assert pages[0]["text"].startswith(source[0][0] + "\r\n" + source[0][1])
    assert pages[1]["text"].startswith(source[1][0])
    raw = len(run.stdout)
    assert raw < 256 * 1024, raw
    # Caps above the ceilings are clamped to them.
    over = read(F.cjk_pdf(source), "--pages", "1-3", "--chars-page", "99999", "--chars-call", "99999")
    assert [len(p["text"]) for p in over.result["pages"]] == [4000, 4000, 0]
    assert "\\u" not in run.stdout.decode("utf-8")


def test_smaller_caps_from_the_host_are_honoured(read):
    run = read(F.latin_pdf([["abcdefghij" * 3], ["klmnopqrst" * 3]]), "--chars-page", "20", "--chars-call", "25",
               "--images", "0")
    pages = run.result["pages"]
    assert [p["text"] for p in pages] == ["abcdefghij" * 2, "klmno"]
    assert [p["truncated"] for p in pages] == [True, True]
    assert [p["chars"] for p in pages] == [30, 30]


def test_a_page_under_the_caps_is_not_truncated(read):
    run = read(F.latin_pdf([["short"]]), "--chars-page", "5")
    assert run.result["pages"][0]["truncated"] is False


# -- the sandbox -------------------------------------------------------------

PROBE = r"""
import ctypes, importlib.util, json, os, resource, socket, sys
script, source, out_dir, outside, port = sys.argv[1:6]
records = []

def probe():
    libc = ctypes.CDLL(None)
    rec = {"limits": {name: list(resource.getrlimit(getattr(resource, name)))
                      for name in ("RLIMIT_CPU", "RLIMIT_AS", "RLIMIT_FSIZE", "RLIMIT_NOFILE", "RLIMIT_CORE", "RLIMIT_NPROC")},
           "no_new_privs": libc.prctl(39, 0, 0, 0, 0)}
    def attempt(fn):
        try:
            fn()
            return "ok"
        except OSError as exc:
            return type(exc).__name__
    rec["read_outside"] = attempt(lambda: open("/etc/passwd", "rb").close())
    rec["write_outside"] = attempt(lambda: open(outside, "wb").close())
    rec["write_out_dir"] = attempt(lambda: open(os.path.join(out_dir, "probe.tmp"), "wb").close())
    rec["read_input"] = attempt(lambda: open(source, "rb").close())
    def connect():
        s = socket.socket()
        try:
            s.settimeout(5)
            s.connect(("127.0.0.1", int(port)))
        finally:
            s.close()
    rec["tcp_connect"] = attempt(connect)
    return rec

class Watch:
    def find_spec(self, name, path=None, target=None):
        if name in ("pypdfium2", "PIL") and not any(r["module"] == name for r in records):
            records.append(dict(probe(), module=name))
        return None

spec = importlib.util.spec_from_file_location("pdf_read_probe", script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
sys.meta_path.insert(0, Watch())
inherited = list(resource.getrlimit(resource.RLIMIT_CPU))
status = module.main([source, out_dir, "--images", "0"])
sys.stderr.write("PROBE " + json.dumps({"status": status, "records": records, "inherited_cpu": inherited}) + "\n")
"""


def _landlock_abi():
    """The landlock ABI this kernel and container offer to a process here (0: none)."""
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    abi = libc.syscall(ctypes.c_long(444), None, ctypes.c_size_t(0), ctypes.c_uint32(1))
    return max(abi, 0)


def _seccomp_available():
    """Whether a process here may install a seccomp filter (x86_64, and no policy refusing it):
    an allow-everything filter, installed in a throwaway child."""
    if os.uname().machine != "x86_64":
        return False
    code = ("import ctypes, sys\n"
            "libc = ctypes.CDLL(None)\n"
            "libc.prctl.argtypes = [ctypes.c_int] + [ctypes.c_ulong] * 4\n"
            "assert libc.prctl(38, 1, 0, 0, 0) == 0\n"
            "class F(ctypes.Structure): _fields_ = [('c', ctypes.c_uint16), ('t', ctypes.c_uint8), ('f', ctypes.c_uint8), ('k', ctypes.c_uint32)]\n"
            "class P(ctypes.Structure): _fields_ = [('n', ctypes.c_uint16), ('p', ctypes.POINTER(F))]\n"
            "prog = (F * 1)(F(6, 0, 0, 0x7FFF0000))\n"
            "sys.exit(0 if libc.prctl(22, 2, ctypes.addressof(P(1, prog)), 0, 0) == 0 else 1)\n")
    return subprocess.run([sys.executable, "-I", "-c", code], timeout=30).returncode == 0


def test_limits_and_landlock_are_in_place_before_the_libraries_load(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    source = tmp_path / "in.pdf"
    source.write_bytes(F.latin_pdf())
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    try:
        proc = subprocess.run([sys.executable, "-I", "-c", PROBE, str(SCRIPT), str(source), str(out),
                               str(tmp_path / "outside.txt"), str(listener.getsockname()[1])],
                              capture_output=True, env=_env(tmp_path), cwd=tmp_path, timeout=60)
    finally:
        listener.close()
    result = json.loads(proc.stdout)
    line = [x for x in proc.stderr.decode().splitlines() if x.startswith("PROBE ")]
    assert line, proc.stderr.decode()[-3000:]
    probe = json.loads(line[-1][len("PROBE "):])
    assert probe["status"] == 0
    # The child must set the limits itself: it may not start out with them already.
    assert probe["inherited_cpu"] != [20, 22], "the limits were inherited, so this probe proves nothing"
    names = [r["module"] for r in probe["records"]]
    assert names[:1] == ["pypdfium2"] and "PIL" in names
    gib = 1024 ** 3
    for record in probe["records"]:
        limits = record["limits"]
        assert limits["RLIMIT_CPU"] == [20, 22]
        assert limits["RLIMIT_CPU"][0] < limits["RLIMIT_CPU"][1]
        assert limits["RLIMIT_AS"] == [int(1.5 * gib)] * 2
        assert limits["RLIMIT_FSIZE"] == [8 * 1024 ** 2] * 2
        assert limits["RLIMIT_NOFILE"] == [64, 64]
        assert limits["RLIMIT_CORE"] == [0, 0]
        assert limits["RLIMIT_NPROC"] == [0, 0]
        assert record["no_new_privs"] == 1
        assert record["read_input"] == "ok" and record["write_out_dir"] == "ok"
        sandbox = result["sandbox"]
        assert sandbox in SANDBOXES
        layers = sandbox.split("+")
        if layers[0] in ("landlock", "landlock_fs"):
            assert record["read_outside"] == "PermissionError", record
            assert record["write_outside"] == "PermissionError", record
        if layers[0] == "landlock" or "seccomp" in layers:
            assert record["tcp_connect"] == "PermissionError", record
    if result["sandbox"].startswith("landlock"):
        assert not (tmp_path / "outside.txt").exists()
    # Either state is acceptable (a container's seccomp may refuse landlock), but the
    # reported state must be the one this environment really offers.
    abi = _landlock_abi()
    layers = ["landlock" if abi >= 4 else "landlock_fs" if abi >= 1 else None, "seccomp" if _seccomp_available() else None]
    assert result["sandbox"] == ("+".join(x for x in layers if x) or "none"), (abi, layers)
    print("sandbox reported:", result["sandbox"], "landlock abi:", abi)


HIJACK = r"""
import importlib.util, json, os, signal, socket, sys
script, out_dir, unix_path, victim = sys.argv[1:5]
spec = importlib.util.spec_from_file_location("pdf_read_hijack", script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
sandbox = module.harden(None, out_dir)
# From here on this is what a child taken over by a hostile PDF could try.
def attempt(fn):
    try:
        fn()
        return "ok"
    except OSError as exc:
        return type(exc).__name__
families = {name: getattr(socket, name) for name in ("AF_UNIX", "AF_INET", "AF_INET6", "AF_NETLINK", "AF_PACKET")
            if hasattr(socket, name)}
rec = {"sandbox": sandbox}
for name, family in families.items():
    for kind in ("SOCK_STREAM", "SOCK_DGRAM", "SOCK_RAW"):
        rec[name + " " + kind] = attempt(lambda: socket.socket(family, getattr(socket, kind)).close())
rec["socketpair"] = attempt(lambda: [s.close() for s in socket.socketpair()])
def unix_connect():
    s = socket.socket(socket.AF_UNIX)
    try:
        s.connect(unix_path)
    finally:
        s.close()
rec["unix_pathname_connect"] = attempt(unix_connect)
rec["kill_other"] = attempt(lambda: os.kill(int(victim), signal.SIGKILL))
rec["kill_all"] = attempt(lambda: os.kill(-1, 0))
rec["kill_self"] = attempt(lambda: os.kill(os.getpid(), 0))
def fork():
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)
rec["fork"] = attempt(fork)
rec["execve"] = attempt(lambda: os.execv("/bin/true", ["/bin/true"]))
print(json.dumps(rec))
"""


def test_a_hijacked_child_cannot_open_a_socket_of_any_family_signal_fork_or_exec(tmp_path):
    """Review SEC-1: under landlock ABI 4 alone a child still reached pathname UNIX sockets (the
    docker socket, the session bus), UDP and other processes. With the seccomp layer reported,
    every socket family, socketpair, a signal to another process, fork and exec are refused."""
    if not _seccomp_available():
        pytest.skip("SKIPPED LOUDLY: this environment refuses a seccomp filter, so the reader reports no seccomp layer")
    out = tmp_path / "out"
    out.mkdir()
    unix_path = str(tmp_path / "s.sock")
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(unix_path)
    listener.listen(1)
    victim = subprocess.Popen(["/bin/sleep", "60"], start_new_session=True)
    try:
        proc = subprocess.run([sys.executable, "-I", "-c", HIJACK, str(SCRIPT), str(out), unix_path, str(victim.pid)],
                              capture_output=True, env=_env(tmp_path), cwd=tmp_path, timeout=60)
        assert proc.returncode == 0, proc.stderr.decode()[-3000:]
        rec = json.loads(proc.stdout)
        assert victim.poll() is None, "the sandboxed child killed a process that is not its own"
    finally:
        victim.kill()
        victim.wait()
        listener.close()
    assert "seccomp" in rec.pop("sandbox")
    assert rec.pop("kill_self") == "ok", "a signal to itself stays allowed (abort, raise)"
    refused = {key: value for key, value in rec.items() if value != "PermissionError"}
    assert not refused, refused
    for family in ("AF_UNIX", "AF_INET", "AF_INET6"):
        for kind in ("SOCK_STREAM", "SOCK_DGRAM"):
            assert f"{family} {kind}" in rec


HIJACK_OWNER = r"""
import fcntl, importlib.util, json, os, socket, struct, sys, termios
script, out_dir = sys.argv[1:3]
victims = dict(zip(("setown", "setown_ex", "setown_pgrp", "fiosetown", "siocspgrp"), map(int, sys.argv[3].split(","))))
spec = importlib.util.spec_from_file_location("pdf_read_owner", script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
inherited = socket.socketpair()  # like the host's stdout and stderr: a socket made before harden
sandbox = module.harden(None, out_dir)
F_SETOWN_EX, F_OWNER_PID, F_SETSIG, FIOSETOWN, SIOCSPGRP = 15, 1, 10, 0x8901, 0x8902
def attempt(fn):
    try:
        fn()
        return "ok"
    except OSError as exc:
        return type(exc).__name__
rec = {"sandbox": sandbox}
def pipe_owner(name, set_owner):
    # Make another process the owner of a pipe's read end, then O_ASYNC and a write: SIGIO goes to the owner.
    r, w = os.pipe()
    rec[name] = attempt(lambda: set_owner(r))
    rec["F_SETFL O_ASYNC " + name] = attempt(lambda: fcntl.fcntl(r, fcntl.F_SETFL, fcntl.fcntl(r, fcntl.F_GETFL) | os.O_ASYNC))
    rec["write " + name] = attempt(lambda: os.write(w, b"x"))
    rec["ioctl FIONREAD " + name] = attempt(lambda: fcntl.ioctl(r, termios.FIONREAD, b"\0\0\0\0"))
pipe_owner("setown", lambda fd: fcntl.fcntl(fd, fcntl.F_SETOWN, victims["setown"]))
pipe_owner("setown_ex", lambda fd: fcntl.fcntl(fd, F_SETOWN_EX, struct.pack("ii", F_OWNER_PID, victims["setown_ex"])))
pipe_owner("setown_pgrp", lambda fd: fcntl.fcntl(fd, fcntl.F_SETOWN, -victims["setown_pgrp"]))
r, w = os.pipe()
rec["setsig"] = attempt(lambda: fcntl.fcntl(r, F_SETSIG, 9))
def socket_owner(name, request, owner):
    a, b = inherited
    rec[name] = attempt(lambda: fcntl.ioctl(a.fileno(), request, struct.pack("i", owner)))
    rec["ioctl FIOASYNC " + name] = attempt(lambda: fcntl.ioctl(a.fileno(), termios.FIOASYNC, struct.pack("i", 1)))
    rec["write " + name] = attempt(lambda: os.write(b.fileno(), b"x"))
socket_owner("fiosetown", FIOSETOWN, victims["fiosetown"])
socket_owner("siocspgrp", SIOCSPGRP, -victims["siocspgrp"])
rec["F_GETOWN"] = attempt(lambda: fcntl.fcntl(inherited[0].fileno(), fcntl.F_GETOWN))
print(json.dumps(rec))
"""


def test_a_hijacked_child_cannot_make_another_process_a_file_owner(tmp_path):
    """Recheck CHK-1: kill and its kin were refused, but fcntl F_SETOWN / F_SETOWN_EX (a pid or a
    process group) with O_ASYNC and a write, and ioctl FIOSETOWN / SIOCSPGRP on a socket, sent
    SIGIO (or F_SETSIG's signal) to a process outside the sandbox. Each owner command is refused;
    F_GETFL/F_SETFL, F_GETOWN and the ordinary ioctls (FIONREAD, FIOASYNC) still work."""
    if not _seccomp_available():
        pytest.skip("SKIPPED LOUDLY: this environment refuses a seccomp filter, so the reader reports no seccomp layer")
    out = tmp_path / "out"
    out.mkdir()
    # Siblings of the child, each the leader of its own session and process group; SIGIO ends a sleep.
    victims = [subprocess.Popen(["/bin/sleep", "60"], start_new_session=True) for _ in range(5)]
    try:
        proc = subprocess.run([sys.executable, "-I", "-c", HIJACK_OWNER, str(SCRIPT), str(out), ",".join(str(v.pid) for v in victims)],
                              capture_output=True, env=_env(tmp_path), cwd=tmp_path, timeout=60)
        assert proc.returncode == 0, proc.stderr.decode()[-3000:]
        rec = json.loads(proc.stdout)
        time.sleep(0.3)  # a signal sent would have landed by now
        killed = [v.pid for v in victims if v.poll() is not None]
    finally:
        for v in victims:
            v.kill()
            v.wait()
    assert "seccomp" in rec.pop("sandbox")
    owners = ("setown", "setown_ex", "setown_pgrp", "setsig", "fiosetown", "siocspgrp")
    refused = {key: rec.pop(key) for key in owners}
    assert refused == {key: "PermissionError" for key in owners}, refused
    assert not killed, f"the sandboxed child signalled {killed} through a file owner"
    assert rec and set(rec.values()) == {"ok"}, rec


HIJACK_LIMITS = r"""
import ctypes, errno, importlib.util, json, os, resource, sys
script, out_dir, sibling = sys.argv[1], sys.argv[2], int(sys.argv[3])
spec = importlib.util.spec_from_file_location("pdf_read_limits", script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
sandbox = module.harden(None, out_dir)
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
L, P = ctypes.c_long, ctypes.c_void_p
def sys_(*args):
    if libc.syscall(*args) < 0:
        raise OSError(ctypes.get_errno(), "")
def attempt(fn):
    try:
        fn()
        return "ok"
    except OSError as exc:
        return errno.errorcode.get(exc.errno, str(exc.errno))
class Rlimit(ctypes.Structure):
    _fields_ = [("cur", ctypes.c_uint64), ("max", ctypes.c_uint64)]
class SchedAttr(ctypes.Structure):  # SCHED_ATTR_SIZE_VER0
    _fields_ = [("size", ctypes.c_uint32), ("policy", ctypes.c_uint32), ("flags", ctypes.c_uint64),
                ("nice", ctypes.c_int32), ("priority", ctypes.c_uint32), ("runtime", ctypes.c_uint64),
                ("deadline", ctypes.c_uint64), ("period", ctypes.c_uint64)]
one_second, mask, head, size = Rlimit(1, 1), ctypes.c_ulong(1), ctypes.c_void_p(), ctypes.c_size_t()
idle = SchedAttr(ctypes.sizeof(SchedAttr), os.SCHED_IDLE, 0, 19, 0, 0, 0, 0)
IOPRIO_IDLE, IOPRIO_BE4, NOBODY_UID = 3 << 13, (2 << 13) | 4, 54321
rec = {"sandbox": sandbox}
# From here on this is what a child taken over by a hostile PDF could try, at the host (our parent) and a sibling.
for name, pid in (("parent", os.getppid()), ("sibling", sibling)):
    rec[name] = {
        "prlimit64 RLIMIT_CPU 1s": attempt(lambda: sys_(L(302), L(pid), L(resource.RLIMIT_CPU), ctypes.byref(one_second), P(None))),
        "prlimit64 read": attempt(lambda: sys_(L(302), L(pid), L(resource.RLIMIT_CPU), P(None), ctypes.byref(Rlimit()))),
        "setpriority 19": attempt(lambda: os.setpriority(os.PRIO_PROCESS, pid, 19)),
        "sched_setaffinity": attempt(lambda: os.sched_setaffinity(pid, {0})),
        "sched_setscheduler SCHED_IDLE": attempt(lambda: os.sched_setscheduler(pid, os.SCHED_IDLE, os.sched_param(0))),
        "sched_setparam": attempt(lambda: os.sched_setparam(pid, os.sched_param(0))),
        "sched_setattr SCHED_IDLE": attempt(lambda: sys_(L(314), L(pid), ctypes.byref(idle), L(0))),
        "ioprio_set idle": attempt(lambda: sys_(L(251), L(1), L(pid), L(IOPRIO_IDLE))),
        "migrate_pages": attempt(lambda: sys_(L(256), L(pid), L(64), ctypes.byref(mask), ctypes.byref(mask))),
        "move_pages": attempt(lambda: sys_(L(279), L(pid), L(0), P(None), P(None), P(None), L(0))),
        "kcmp": attempt(lambda: sys_(L(312), L(pid), L(os.getpid()), L(0), L(0), L(0))),
        "get_robust_list": attempt(lambda: sys_(L(274), L(pid), ctypes.byref(head), ctypes.byref(size))),
    }
# Our process group is the parent's; a user nobody runs as gives ESRCH, not EPERM, without the filter.
rec["group"] = {
    "setpriority PRIO_PGRP": attempt(lambda: os.setpriority(os.PRIO_PGRP, 0, 19)),
    "setpriority PRIO_USER": attempt(lambda: os.setpriority(os.PRIO_USER, NOBODY_UID, 19)),
    "ioprio_set IOPRIO_WHO_PGRP": attempt(lambda: sys_(L(251), L(2), L(0), L(IOPRIO_IDLE))),
    "ioprio_set IOPRIO_WHO_USER": attempt(lambda: sys_(L(251), L(3), L(NOBODY_UID), L(IOPRIO_IDLE))),
    "process_madvise": attempt(lambda: sys_(L(440), L(-1), P(None), L(0), L(0), L(0))),
    "process_mrelease": attempt(lambda: sys_(L(448), L(-1), L(0))),
    "perf_event_open": attempt(lambda: sys_(L(298), P(None), L(-1), L(0), L(-1), L(0))),
    "keyctl": attempt(lambda: sys_(L(250), L(0), L(-4), L(0))),  # KEYCTL_GET_KEYRING_ID, the user keyring
    "msgget": attempt(lambda: sys_(L(68), L(0), L(0o600))),
    "semget": attempt(lambda: sys_(L(64), L(0), L(1), L(0o600))),
    "shmget": attempt(lambda: sys_(L(29), L(0), L(4096), L(0o600))),
}
# Its own limits and scheduling stay this process's to change.
rec["self"] = {
    "setrlimit": attempt(lambda: resource.setrlimit(resource.RLIMIT_CORE, (0, 0))),
    "prlimit64 pid 0": attempt(lambda: sys_(L(302), L(0), L(resource.RLIMIT_CPU), ctypes.byref(Rlimit(19, 22)), P(None))),
    "prlimit64 own pid": attempt(lambda: sys_(L(302), L(os.getpid()), L(resource.RLIMIT_CPU), P(None), ctypes.byref(Rlimit()))),
    "setpriority": attempt(lambda: os.setpriority(os.PRIO_PROCESS, 0, 1)),
    "nice": attempt(lambda: os.nice(1)),
    "sched_setaffinity": attempt(lambda: os.sched_setaffinity(0, os.sched_getaffinity(0))),
    "sched_setscheduler": attempt(lambda: os.sched_setscheduler(os.getpid(), os.SCHED_OTHER, os.sched_param(0))),
    "sched_setparam": attempt(lambda: os.sched_setparam(0, os.sched_param(0))),
    "ioprio_set": attempt(lambda: sys_(L(251), L(1), L(0), L(IOPRIO_BE4))),
}
rec["self_cpu_limit"] = list(resource.getrlimit(resource.RLIMIT_CPU))
print(json.dumps(rec))
"""

# The host and a sibling stand-in: note its own state, (the host) run the hijacked child, then burn
# 1.5 s of CPU, past an RLIMIT_CPU of {1, 1} had the child set one, and note its state again.
VICTIM = r"""
import ctypes, json, os, resource, subprocess, sys, time
libc = ctypes.CDLL(None)
libc.syscall.restype = ctypes.c_long
def state():
    return {"nice": os.getpriority(os.PRIO_PROCESS, 0), "policy": os.sched_getscheduler(0),
            "affinity": sorted(os.sched_getaffinity(0)), "ioprio": libc.syscall(ctypes.c_long(252), ctypes.c_long(1), ctypes.c_long(0)),
            "cpu_limit": list(resource.getrlimit(resource.RLIMIT_CPU))}
out = {"before": state()}
if len(sys.argv) > 1:
    child = subprocess.run([sys.executable, "-I", "-c", sys.argv[1], *sys.argv[2:]], capture_output=True, timeout=60)
    out["child_rc"], out["child"], out["child_err"] = child.returncode, child.stdout.decode(), child.stderr.decode()[-3000:]
else:
    sys.stdin.readline()
start = time.process_time()
while time.process_time() - start < 1.5:
    pass
out["after"] = state()
print(json.dumps(out))
"""


def test_a_hijacked_child_cannot_change_another_processs_limits_or_scheduling(tmp_path):
    """Recheck CHK-5 and CHK-6: prlimit64 at the host (getppid()) set RLIMIT_CPU {1, 1} and the
    kernel killed the host; setpriority, sched_setaffinity and sched_setscheduler slowed another
    process for good. Each, with sched_setparam, sched_setattr, ioprio_set, the process group and
    user forms and the other calls that reach another process, is refused at the host and a
    sibling, and both burn past one second of CPU and live with their state unchanged; the child
    still changes its own limits and scheduling (setrlimit is prlimit64 at pid 0)."""
    if not _seccomp_available():
        pytest.skip("SKIPPED LOUDLY: this environment refuses a seccomp filter, so the reader reports no seccomp layer")
    out = tmp_path / "out"
    out.mkdir()
    sibling = subprocess.Popen([sys.executable, "-I", "-c", VICTIM], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               start_new_session=True)
    try:
        # The host stand-in leads its own session and process group, as the host's detached spawn does.
        host = subprocess.run([sys.executable, "-I", "-c", VICTIM, HIJACK_LIMITS, str(SCRIPT), str(out), str(sibling.pid)],
                              capture_output=True, env=_env(tmp_path), cwd=tmp_path, timeout=120, start_new_session=True)
        sib_out, _ = sibling.communicate(b"\n", timeout=60)
    finally:
        sibling.kill()
        sibling.wait()
    assert host.returncode == 0, f"the host stand-in died: rc {host.returncode} {host.stderr.decode()[-2000:]}"
    assert sibling.returncode == 0, f"the sibling died: rc {sibling.returncode}"
    host_out, sib_out = json.loads(host.stdout), json.loads(sib_out)
    assert host_out["child_rc"] == 0, host_out["child_err"]
    rec = json.loads(host_out["child"])
    print(json.dumps(rec, indent=1))
    assert "seccomp" in rec.pop("sandbox")
    for name in ("parent", "sibling", "group"):
        refused = {key: value for key, value in rec[name].items() if value != "EPERM"}
        assert not refused, (name, refused)
    assert set(rec["self"].values()) == {"ok"}, rec["self"]
    assert rec["self_cpu_limit"] == [19, 22]
    for who, victim in (("host", host_out), ("sibling", sib_out)):
        assert victim["after"] == victim["before"], (who, victim)


def test_the_seccomp_filter_is_well_formed():
    module = _load_module()
    program = module.seccomp_program(4242)
    assert len(program) < 256
    for i, (code, jt, jf, k) in enumerate(program):
        assert 0 <= jt < 256 and 0 <= jf < 256 and 0 <= k < 2 ** 32
        if code != module.BPF_RET_K:
            assert i + 1 + max(jt, jf) < len(program), "a jump past the end"
    assert program[-1][0] == module.BPF_RET_K and program[0] == (module.BPF_LD_W_ABS, 0, 0, 4)


def test_the_script_imports_only_the_standard_library_pypdfium2_and_pil():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    allowed = set(sys.stdlib_module_names) | {"pypdfium2", "PIL"}
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative imports"
            found.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call) and getattr(node.func, "id", None) == "__import__":
            pytest.fail("no dynamic imports")
    assert found <= allowed, found - allowed
    # The native libraries load only in load_libraries(), which main() calls after harden().
    top = {alias.name.split(".")[0] for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
           for alias in (node.names if isinstance(node, ast.Import) else [ast.alias(node.module)])}
    assert not top & {"pypdfium2", "PIL"}
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    calls = [n.func.id for n in ast.walk(main) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert calls.index("harden") < calls.index("load_libraries")
    assert "init_forms" not in SCRIPT.read_text(encoding="utf-8")


def test_selfcheck_passes_and_reports_the_sandbox(tmp_path):
    proc = subprocess.run([sys.executable, "-I", str(SCRIPT), "--selfcheck", str(tmp_path)],
                          capture_output=True, env=_env(tmp_path), cwd=tmp_path, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr[-2000:]
    result = json.loads(proc.stdout)
    _assert_no_path_key(result)
    assert result["selfcheck"] is True and result["sandbox"] in SANDBOXES
    assert result["pages"][0]["image"] is True
    assert sorted(os.listdir(tmp_path)) == ["p1.jpg"]
    bare = subprocess.run([sys.executable, "-I", str(SCRIPT), "--selfcheck"],
                          capture_output=True, env=_env(tmp_path), cwd="/", timeout=60)
    assert bare.returncode == 0 and json.loads(bare.stdout)["selfcheck"] is True


def test_under_isolated_mode_the_script_directory_is_not_on_sys_path(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    shutil.copy(SCRIPT, home / "pdf_read.py")
    # A planted module beside the script: it would win the import if the script's
    # directory were on sys.path. The output directory is the same directory, so the
    # sandbox lets the import system see it.
    (home / "pypdfium2.py").write_text("import sys\nsys.stdout.write('PLANTED\\n')\nsys.exit(99)\n")
    isolated = subprocess.run([sys.executable, "-I", str(home / "pdf_read.py"), "--selfcheck", str(home)],
                              capture_output=True, env=_env(home), cwd=home, timeout=60)
    assert isolated.returncode == 0 and b"PLANTED" not in isolated.stdout, isolated.stdout + isolated.stderr[-2000:]
    (home / "p1.jpg").unlink()
    # Control: without -I the plant is found, so the check above is a real one.
    plain = subprocess.run([sys.executable, str(home / "pdf_read.py"), "--selfcheck", str(home)],
                           capture_output=True, env=_env(home), cwd=home, timeout=60)
    assert plain.returncode == 99 and b"PLANTED" in plain.stdout
