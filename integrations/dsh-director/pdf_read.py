"""Read selected pages of one PDF: the text layer, and optionally a JPEG per page.

The host runs this as a short-lived child, `python -I pdf_read.py <input> <outdir> ...`,
in a private temporary directory, and trusts nothing it reports beyond one JSON line.
The PDF may come from a stranger, so before the native parser is loaded the child
limits itself:

  1. resource limits: CPU soft 20 s / hard 22 s, address space 1.5 GiB, files
     written 8 MiB (each), 64 descriptors, no core dumps, no new process (NPROC 0,
     which a root user is not held to);
  2. PR_SET_NO_NEW_PRIVS;
  3. landlock: read (never execute) only the interpreter's prefixes, the system
     library and font directories and the input file; write only in the output
     directory; on landlock ABI 4 and later also TCP bind and connect denied.
     Landlock does not cover UNIX sockets (pathname or abstract), UDP, signals or
     new processes: measured on kernel 6.8 (ABI 4), all of those stay open under it;
  4. seccomp (x86_64 only), which closes those: socket and socketpair of every
     family, connect, bind, listen, accept, sendto/sendmsg/sendmmsg, io_uring,
     execve/execveat, fork/vfork and a clone that is not a thread, unshare/setns,
     ptrace, process_vm_readv/writev, pidfd_open/getfd/send_signal, kill, tkill,
     tgkill and rt_(tg)sigqueueinfo at any process but this one, and a file owner set
     to another process (fcntl F_SETOWN, F_SETOWN_EX, F_SETSIG; ioctl FIOSETOWN,
     SIOCSPGRP: SIGIO or F_SETSIG's signal would go to that owner) fail with EPERM
     (clone3 with ENOSYS, so a thread falls back to clone). With no owner set, O_ASYNC
     signals nobody; F_NOTIFY and F_SETLEASE make the caller itself the owner.
  What stays open even with every layer: reading inside the allowed paths, writing
  files of up to 8 MiB each in the output directory until the CPU limit, the
  descriptors the host gave (stdout and stderr), and System V IPC (any message queue,
  semaphore set or shared memory segment in the IPC namespace whose mode lets
  this user in). Each layer fails open: the result's
  `sandbox` names exactly the layers in place, joined by "+" - "landlock" (files and
  TCP) or "landlock_fs" (files only), then "seccomp" - or "none", and the read goes on.

Only then are pypdfium2 and PIL imported. PDF forms are never initialised.

stdout is one line of JSON (ensure_ascii=False), with no path in it:
  {"page_count", "sandbox", "pages": [{"n", "text", "chars", "truncated",
   "no_text_layer", "image", "width"?, "height"?, "error"?}]}
  `chars` is the page's whole character count; `text` holds at most the caps.
or, with a non-zero exit, {"error": "encrypted" | "unsupported_security" | "broken"
  | "page_out_of_range" | "too_many_pages" | "invalid_request", "sandbox", "page_count"?}.

`--selfcheck [outdir]` renders a one-page PDF built here in memory and reports the
sandbox state. This file imports only the standard library, pypdfium2 and PIL.
"""
import argparse
import ctypes
import io
import json
import math
import os
import resource
import sys

# Hard ceilings. The host passes the working values; these clamp them whatever it says.
MAX_DPI = 150
MAX_LONG_SIDE = 1540
MAX_PIXELS = 2_500_000
MAX_CHARS_PAGE = 4000
MAX_CHARS_CALL = 8000
MAX_PAGES_WITH_IMAGES = 4
MAX_PAGES_TEXT_ONLY = 30
JPEG_SOFT_LIMIT = 512 * 1024
JPEG_FALLBACK_LONG_SIDE = 1280

LIMITS = (
    (resource.RLIMIT_CPU, 20, 22),
    (resource.RLIMIT_AS, 1536 * 1024 * 1024, 1536 * 1024 * 1024),
    (resource.RLIMIT_FSIZE, 8 * 1024 * 1024, 8 * 1024 * 1024),
    (resource.RLIMIT_NOFILE, 64, 64),
    (resource.RLIMIT_CORE, 0, 0),
    (resource.RLIMIT_NPROC, 0, 0),
)
READ_ROOTS = ("/usr/lib", "/lib", "/lib64", "/usr/share/fonts")
SELFCHECK_TEXT = "pdf reader selfcheck 4721"

# Filled by load_libraries(), after the sandbox is in place.
pdfium = None
pdfium_c = None
Image = None


def set_limits():
    """Lower every limit to its ceiling; a limit already lower than ours stays."""
    for which, soft, hard in LIMITS:
        _, cur_hard = resource.getrlimit(which)
        if cur_hard != resource.RLIM_INFINITY:
            hard = min(hard, cur_hard)
        resource.setrlimit(which, (min(soft, hard), hard))


# landlock (include/uapi/linux/landlock.h). The syscall numbers are the same on
# every architecture.
SYS_CREATE_RULESET, SYS_ADD_RULE, SYS_RESTRICT_SELF = 444, 445, 446
CREATE_RULESET_VERSION = 1
RULE_PATH_BENEATH = 1
FS_EXECUTE, FS_WRITE_FILE, FS_READ_FILE, FS_READ_DIR = 1 << 0, 1 << 1, 1 << 2, 1 << 3
FS_REMOVE_FILE, FS_MAKE_REG, FS_TRUNCATE = 1 << 5, 1 << 8, 1 << 14
NET_BIND_TCP, NET_CONNECT_TCP = 1 << 0, 1 << 1
PR_SET_NO_NEW_PRIVS = 38

# seccomp (linux/seccomp.h, linux/filter.h, linux/audit.h). The numbers are x86_64's; on any
# other machine no filter is installed.
PR_SET_SECCOMP, SECCOMP_MODE_FILTER = 22, 2
SECCOMP_RET_ALLOW, SECCOMP_RET_ERRNO = 0x7FFF0000, 0x00050000
EPERM, ENOSYS = 1, 38
AUDIT_ARCH_X86_64, X32_SYSCALL_BIT, CLONE_THREAD = 0xC000003E, 0x40000000, 0x00010000
BPF_LD_W_ABS, BPF_JEQ_K, BPF_JGE_K, BPF_JSET_K, BPF_RET_K = 0x20, 0x15, 0x35, 0x45, 0x06
# Refused outright: no socket of any family, no exec, no new process, no reaching into another.
DENIED_SYSCALLS = (
    41, 42, 43, 44, 46, 49, 50, 53, 288, 307,  # socket connect accept sendto sendmsg bind listen socketpair accept4 sendmmsg
    425, 426, 427,  # io_uring_setup/enter/register (io_uring can open sockets past this filter)
    57, 58, 59, 322, 272, 308,  # fork vfork execve execveat unshare setns
    101, 310, 311, 424, 434, 438,  # ptrace process_vm_readv/writev pidfd_send_signal pidfd_open pidfd_getfd
)
# A signal only to this process (its first argument, a 32-bit pid or tgid, must be ours).
SELF_ONLY_SYSCALLS = (62, 129, 200, 234, 297)  # kill rt_sigqueueinfo tkill tgkill rt_tgsigqueueinfo
SYS_CLONE, SYS_CLONE3 = 56, 435
# A process to signal through a file's owner: refused by command, every other fcntl and ioctl stays.
# Both calls take cmd as an unsigned int, so only args[1]'s low word counts. x86_64 has no fcntl64.
SYS_FCNTL, SYS_IOCTL = 72, 16
OWNER_FCNTLS = (8, 15, 10)  # F_SETOWN F_SETOWN_EX F_SETSIG
OWNER_IOCTLS = (0x8901, 0x8902)  # FIOSETOWN SIOCSPGRP


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64), ("handled_access_net", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def _handled_fs(abi):
    mask = (1 << 13) - 1           # ABI 1: execute .. make_sym
    if abi >= 2:
        mask |= 1 << 13            # refer
    if abi >= 3:
        mask |= FS_TRUNCATE
    if abi >= 5:
        mask |= 1 << 15            # ioctl_dev
    return mask


def _landlock(syscall, input_path, out_dir):
    """The landlock layer: "landlock" (files and TCP), "landlock_fs" (files only: ABI < 4) or None."""
    abi = syscall(ctypes.c_long(SYS_CREATE_RULESET), None, ctypes.c_size_t(0), ctypes.c_uint32(CREATE_RULESET_VERSION))
    if abi < 1:
        return None
    handled = _handled_fs(abi)
    attr = _RulesetAttr(handled, NET_BIND_TCP | NET_CONNECT_TCP if abi >= 4 else 0)
    size = ctypes.sizeof(_RulesetAttr) if abi >= 4 else 8
    ruleset = syscall(ctypes.c_long(SYS_CREATE_RULESET), ctypes.byref(attr), ctypes.c_size_t(size), ctypes.c_uint32(0))
    if ruleset < 0:
        return None
    # No execute right: a library is mapped, never executed, and landlock's execute is execve's.
    read_dir = FS_READ_FILE | FS_READ_DIR
    write_dir = FS_READ_FILE | FS_READ_DIR | FS_WRITE_FILE | FS_MAKE_REG | FS_REMOVE_FILE | FS_TRUNCATE
    rules = [(root, read_dir) for root in (sys.prefix, sys.base_prefix, *READ_ROOTS)]
    if input_path is not None:
        rules.append((input_path, FS_READ_FILE))
    if out_dir is not None:
        rules.append((out_dir, write_dir))
    try:
        for path, access in rules:
            try:
                fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            except FileNotFoundError:
                continue
            try:
                rule = _PathBeneathAttr(access & handled, fd)
                if syscall(ctypes.c_long(SYS_ADD_RULE), ctypes.c_int(ruleset), ctypes.c_int(RULE_PATH_BENEATH),
                           ctypes.byref(rule), ctypes.c_uint32(0)) != 0:
                    return None
            finally:
                os.close(fd)
        if syscall(ctypes.c_long(SYS_RESTRICT_SELF), ctypes.c_int(ruleset), ctypes.c_uint32(0)) != 0:
            return None
    finally:
        os.close(ruleset)
    return "landlock" if abi >= 4 else "landlock_fs"


def seccomp_program(pid):
    """The seccomp filter as (code, jt, jf, k): DENIED_SYSCALLS and any non-x86_64 call -> EPERM,
    clone3 -> ENOSYS, clone without CLONE_THREAD -> EPERM, SELF_ONLY_SYSCALLS at another pid -> EPERM,
    fcntl with an OWNER_FCNTLS command and ioctl with an OWNER_IOCTLS command -> EPERM."""
    program = [
        (BPF_LD_W_ABS, 4, 0, 0), (BPF_JEQ_K, AUDIT_ARCH_X86_64, 0, "deny"),  # seccomp_data.arch
        (BPF_LD_W_ABS, 0, 0, 0), (BPF_JGE_K, X32_SYSCALL_BIT, "deny", 0),  # seccomp_data.nr
        *[(BPF_JEQ_K, nr, "deny", 0) for nr in DENIED_SYSCALLS],
        (BPF_JEQ_K, SYS_CLONE3, "enosys", 0),
        (BPF_JEQ_K, SYS_CLONE, "clone", 0),
        *[(BPF_JEQ_K, nr, "self", 0) for nr in SELF_ONLY_SYSCALLS],
        (BPF_JEQ_K, SYS_FCNTL, "fcntl", 0),
        (BPF_JEQ_K, SYS_IOCTL, "ioctl", 0),
        (BPF_RET_K, SECCOMP_RET_ALLOW, 0, 0),
        "fcntl", (BPF_LD_W_ABS, 24, 0, 0),  # args[1] low word: cmd
        *[(BPF_JEQ_K, cmd, "deny", 0) for cmd in OWNER_FCNTLS],
        (BPF_RET_K, SECCOMP_RET_ALLOW, 0, 0),
        "ioctl", (BPF_LD_W_ABS, 24, 0, 0),
        *[(BPF_JEQ_K, cmd, "deny", 0) for cmd in OWNER_IOCTLS],
        (BPF_RET_K, SECCOMP_RET_ALLOW, 0, 0),
        "clone", (BPF_LD_W_ABS, 16, 0, 0), (BPF_JSET_K, CLONE_THREAD, "allow", "deny"),  # args[0] low word: flags
        "self", (BPF_LD_W_ABS, 20, 0, 0), (BPF_JEQ_K, 0, 0, "deny"),  # args[0] high word
        (BPF_LD_W_ABS, 16, 0, 0), (BPF_JEQ_K, pid, "allow", "deny"),
        "allow", (BPF_RET_K, SECCOMP_RET_ALLOW, 0, 0),
        "deny", (BPF_RET_K, SECCOMP_RET_ERRNO | EPERM, 0, 0),
        "enosys", (BPF_RET_K, SECCOMP_RET_ERRNO | ENOSYS, 0, 0),
    ]
    labels, n = {}, 0
    for item in program:
        if isinstance(item, str):
            labels[item] = n
        else:
            n += 1
    out = []
    for item in program:
        if isinstance(item, str):
            continue
        code, k, jt, jf = item
        here = len(out) + 1
        out.append((code, labels[jt] - here if isinstance(jt, str) else jt,
                    labels[jf] - here if isinstance(jf, str) else jf, k))
    return out


class _SockFilter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_uint16), ("jt", ctypes.c_uint8), ("jf", ctypes.c_uint8), ("k", ctypes.c_uint32)]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_uint16), ("filter", ctypes.POINTER(_SockFilter))]


def _seccomp(libc):
    """The seccomp layer: "seccomp" once the filter is in place, else None (another machine, or
    a kernel or container policy that refuses it)."""
    if os.uname().machine != "x86_64":
        return None
    program = seccomp_program(os.getpid())
    filters = (_SockFilter * len(program))(*program)
    prog = _SockFprog(len(program), filters)
    if libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.addressof(prog), 0, 0) != 0:
        return None
    return "seccomp"


def harden(input_path, out_dir):
    """Limits, no_new_privs, landlock, then seccomp. Returns the sandbox state for the report:
    the layers in place joined by "+" ("landlock" or "landlock_fs", then "seccomp"), or "none"."""
    set_limits()
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        return "none"
    syscall = libc.syscall
    syscall.restype = ctypes.c_long
    layers = [_landlock(syscall, input_path, out_dir), _seccomp(libc)]
    return "+".join(layer for layer in layers if layer) or "none"


def load_libraries():
    global pdfium, pdfium_c, Image
    import pypdfium2
    import pypdfium2.raw
    from PIL import Image as image_module
    pdfium, pdfium_c, Image = pypdfium2, pypdfium2.raw, image_module


def open_error(exc):
    """The refusal for a document that failed to open, by PDFium's error code only."""
    code = getattr(exc, "err_code", None)
    if code == pdfium_c.FPDF_ERR_PASSWORD:
        return "encrypted"
    if code == pdfium_c.FPDF_ERR_SECURITY:
        return "unsupported_security"
    return "broken"


def render_scale(width, height, dpi, long_side, max_pixels):
    """min(dpi/72, long/max(w,h), sqrt(px/(w*h))), then nudged down so that the
    rendered size (rounded up, as PDFium does) still fits both caps."""
    scale = min(dpi / 72, long_side / max(width, height), math.sqrt(max_pixels / (width * height)))
    for _ in range(64):
        w, h = math.ceil(width * scale), math.ceil(height * scale)
        if max(w, h) <= long_side and w * h <= max_pixels:
            break
        scale *= 0.999
    return scale


def encode_jpeg(image, limit=JPEG_SOFT_LIMIT):
    """q85; over the limit, q70; still over, shrink to a long side of 1280 at q70."""
    for quality in (85, 70):
        buf = io.BytesIO()
        image.save(buf, "JPEG", quality=quality)
        if buf.tell() <= limit:
            return buf.getvalue(), image.size
    w, h = image.size
    if max(w, h) > JPEG_FALLBACK_LONG_SIDE:
        ratio = JPEG_FALLBACK_LONG_SIDE / max(w, h)
        image = image.resize((max(1, round(w * ratio)), max(1, round(h * ratio))), Image.LANCZOS)
    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=70)
    return buf.getvalue(), image.size


def write_new(out_dir, name, data):
    fd = os.open(os.path.join(out_dir, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def read_page(pdf, n, opts, budget, out_dir):
    """One page's entry. `budget` is a one-item list: characters left in this call."""
    page = pdf[n - 1]
    textpage = page.get_textpage()
    total = textpage.count_chars()
    sample = textpage.get_text_range(0, min(total, opts.chars_page)) if total > 0 else ""
    no_text = not sample.strip()
    entry = {"n": n, "text": "", "chars": 0, "truncated": False, "no_text_layer": no_text, "image": False}
    if not no_text:
        text = sample[:budget[0]]
        budget[0] -= len(text)
        entry.update(text=text, chars=total, truncated=total > len(text))
    if opts.images:
        width, height = page.get_size()
        scale = render_scale(width, height, opts.dpi, opts.long_side, opts.max_pixels)
        bitmap = page.render(scale=scale, grayscale=no_text, may_draw_forms=False)
        image = bitmap.to_pil().convert("L" if no_text else "RGB")
        data, (w, h) = encode_jpeg(image)
        if out_dir is not None:  # only a selfcheck without a directory renders in memory
            write_new(out_dir, "p%d.jpg" % n, data)
        entry.update(image=True, width=w, height=h)
    return entry


def parse_pages(spec, page_count, images):
    """'a-b' or 'n', 1-based. The end is clipped to the document; the start is not."""
    first, sep, last = spec.partition("-")
    if not first.isdigit() or (sep and not last.isdigit()):
        raise ValueError("invalid_request")
    a, b = int(first), int(last) if sep else int(first)
    if a < 1 or b < a:
        raise ValueError("invalid_request")
    if b - a + 1 > (MAX_PAGES_WITH_IMAGES if images else MAX_PAGES_TEXT_ONLY):
        raise ValueError("too_many_pages")
    if a > page_count:
        raise ValueError("page_out_of_range")
    return range(a, min(b, page_count) + 1)


def read_document(source, opts, out_dir, sandbox):
    """The result object, and the exit status."""
    try:
        pdf = pdfium.PdfDocument(source)
    except pdfium.PdfiumError as exc:
        return {"error": open_error(exc), "sandbox": sandbox}, 2
    except Exception:
        return {"error": "broken", "sandbox": sandbox}, 2
    try:
        page_count = len(pdf)
        spec = opts.pages or "1-%d" % (MAX_PAGES_WITH_IMAGES if opts.images else MAX_PAGES_TEXT_ONLY)
        try:
            selected = parse_pages(spec, page_count, opts.images)
        except ValueError as exc:
            result = {"error": str(exc), "sandbox": sandbox}
            if str(exc) == "page_out_of_range":
                result["page_count"] = page_count
            return result, 2
        budget = [opts.chars_call]
        pages = []
        for n in selected:
            try:
                pages.append(read_page(pdf, n, opts, budget, out_dir))
            except Exception:
                pages.append({"n": n, "text": "", "chars": 0, "truncated": False,
                              "no_text_layer": False, "image": False, "error": "broken"})
        return {"page_count": page_count, "sandbox": sandbox, "pages": pages}, 0
    finally:
        pdf.close()


def selfcheck_pdf():
    """A one-page PDF with a known line of Helvetica text, written from bytes."""
    content = b"BT /F1 24 Tf 72 720 Td (%s) Tj ET\n0 0 1 rg 72 600 200 60 re f\n" % SELFCHECK_TEXT.encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for num, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % num + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def emit(result):
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def parse_args(argv):
    p = argparse.ArgumentParser(prog="pdf_read.py", description="Read pages of one PDF (see the module docstring).")
    p.add_argument("--selfcheck", action="store_true")
    p.add_argument("input", nargs="?")
    p.add_argument("out_dir", nargs="?")
    p.add_argument("--pages", default=None)
    p.add_argument("--images", type=int, choices=(0, 1), default=1)
    p.add_argument("--dpi", type=float, default=MAX_DPI)
    p.add_argument("--long-side", type=int, default=MAX_LONG_SIDE)
    p.add_argument("--max-pixels", type=int, default=MAX_PIXELS)
    p.add_argument("--chars-page", type=int, default=MAX_CHARS_PAGE)
    p.add_argument("--chars-call", type=int, default=MAX_CHARS_CALL)
    opts = p.parse_args(argv)
    if opts.selfcheck:
        # One argument means the output directory; the PDF is built in memory.
        opts.out_dir, opts.input = opts.input, None
    elif not opts.input or not opts.out_dir:
        p.error("input and out_dir are required")
    opts.images = bool(opts.images)
    opts.dpi = min(max(opts.dpi, 1.0), MAX_DPI)
    opts.long_side = min(max(opts.long_side, 1), MAX_LONG_SIDE)
    opts.max_pixels = min(max(opts.max_pixels, 1), MAX_PIXELS)
    opts.chars_page = min(max(opts.chars_page, 0), MAX_CHARS_PAGE)
    opts.chars_call = min(max(opts.chars_call, 0), MAX_CHARS_CALL)
    return opts


def main(argv=None):
    opts = parse_args(sys.argv[1:] if argv is None else argv)
    input_path = os.path.abspath(opts.input) if opts.input else None
    out_dir = os.path.abspath(opts.out_dir) if opts.out_dir else None
    sandbox = harden(input_path, out_dir)
    load_libraries()
    if opts.selfcheck:
        opts.pages = "1"
        result, status = read_document(selfcheck_pdf(), opts, out_dir, sandbox)
        ok = status == 0 and result["pages"][0]["text"].strip() == SELFCHECK_TEXT \
            and not result["pages"][0].get("error") and result["pages"][0]["image"]
        if not ok:
            result, status = {"error": "selfcheck_failed", "sandbox": sandbox}, 2
        result["selfcheck"] = ok
        emit(result)
        return status
    result, status = read_document(input_path, opts, out_dir, sandbox)
    emit(result)
    return status


if __name__ == "__main__":
    sys.exit(main())
