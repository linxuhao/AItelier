#!/usr/bin/env python3
"""unity_compile — license-free semantic compile of Unity C# scripts via Roslyn.

Runs INSIDE the unityci/editor image (the `unity-builder` sidecar). It globs
``*.cs`` under a project directory and compiles them as a library against
Unity's bundled reference assemblies using the editor's own Roslyn ``csc`` — no
editor launch, no license activation (only *running* the editor needs a
license; the compiler does not). This catches the bugs LLM-generated Unity code
actually has: hallucinated APIs, wrong signatures, missing usings, type errors.

Two modes:
  python3 unity_compile.py <project_dir>   → print JSON report, exit 0 (pass) / 1 (fail)
  python3 unity_compile.py --serve         → HTTP server on $PORT (default 8080):
                                               POST /compile {"project_dir": "/abs/path"}
                                               GET  /health

The AItelier `csharp` lint backend POSTs to /compile. Both containers bind-mount
~/.AItelier at the same absolute path, so `project_dir` resolves identically on
each side — no file transfer needed.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ── Unity toolchain paths (verified for ubuntu-6000.0.76f1-base) ──────────
UNITY = Path("/opt/unity/Editor/Data")
DOTNET = UNITY / "NetCoreRuntime" / "dotnet"
CSC = UNITY / "DotNetSdkRoslyn" / "csc.dll"
NETSTANDARD = UNITY / "NetStandard" / "ref" / "2.1.0" / "netstandard.dll"
UNITY_MODULES = UNITY / "Managed" / "UnityEngine"
# Built-in package assemblies (UnityEngine.UI / TextMeshPro / InputSystem / URP …)
# are NOT in Data/Managed — they ship as packages. Without them, gameplay code
# using standard packages (e.g. `using UnityEngine.UI;`) false-fails. The editor
# bundles a precompiled set in the cross-platform project template's libcache;
# glob is version-agnostic so it survives editor-patch bumps.
_PKG_ASM_GLOB = ("Resources/PackageManager/ProjectTemplates/libcache/"
                 "com.unity.template.3d-cross-platform-*/ScriptAssemblies")

# Directories that never hold hand-written gameplay scripts to lint.
_SKIP_DIRS = {"Library", "Temp", "obj", "Build", "Builds", ".git"}

# csc diagnostic line: path(line,col): error CS1061: message
_DIAG = re.compile(r"^(?P<file>.+?)\((?P<line>\d+),(?P<col>\d+)\):\s+"
                   r"(?P<sev>error|warning)\s+(?P<code>CS\d+):\s+(?P<msg>.*)$")


def _ref_args() -> list[str]:
    """Build the -r: reference list once: netstandard + Unity modules + packages.

    Deduped by file name (engine modules win over any same-named package copy) so
    csc never sees a duplicate-assembly conflict.
    """
    seen: set[str] = set()
    paths: list = []
    # Core engine + editor module assemblies (authoritative).
    for p in sorted(UNITY_MODULES.glob("*.dll")):
        if p.name not in seen:
            seen.add(p.name)
            paths.append(p)
    # Built-in package assemblies (UI, TextMeshPro, InputSystem, URP, …).
    for d in sorted(UNITY.glob(_PKG_ASM_GLOB)):
        for p in sorted(d.glob("*.dll")):
            if p.name not in seen:
                seen.add(p.name)
                paths.append(p)
    return [f"-r:{NETSTANDARD}"] + [f"-r:{p}" for p in paths]


_REFS = _ref_args()


def compile_project(project_dir: str) -> dict:
    """Compile every .cs under project_dir as a library; return a report dict.

    Returns {passed, returncode, file_count, errors[], warning_count, summary}.
    errors[] = [{file, line, col, code, message}] (relative paths).
    """
    root = Path(project_dir).resolve()
    if not root.is_dir():
        return {"passed": False, "returncode": -1, "file_count": 0,
                "errors": [], "warning_count": 0,
                "summary": f"project_dir not found: {root}"}

    sources = [p for p in root.rglob("*.cs")
               if not (_SKIP_DIRS & set(p.relative_to(root).parts))]
    if not sources:
        return {"passed": True, "returncode": 0, "file_count": 0,
                "errors": [], "warning_count": 0,
                "summary": "No .cs files to compile."}

    with tempfile.TemporaryDirectory() as tmp:
        rsp = Path(tmp) / "compile.rsp"
        out = Path(tmp) / "out.dll"
        lines = ["-nologo", "-nostdlib", "-target:library", f"-out:{out}"]
        lines += _REFS
        lines += [str(p) for p in sources]
        rsp.write_text("\n".join(lines), encoding="utf-8")

        try:
            r = subprocess.run([str(DOTNET), str(CSC), f"@{rsp}"],
                               capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            return {"passed": False, "returncode": -1,
                    "file_count": len(sources), "errors": [], "warning_count": 0,
                    "summary": "csc timed out after 300s"}

    output = (r.stdout or "") + "\n" + (r.stderr or "")
    errors: list[dict] = []
    warnings = 0
    for ln in output.splitlines():
        m = _DIAG.match(ln.strip())
        if not m:
            continue
        if m["sev"] == "warning":
            warnings += 1
            continue
        try:
            rel = str(Path(m["file"]).resolve().relative_to(root))
        except ValueError:
            rel = m["file"]
        errors.append({"file": rel, "line": int(m["line"]),
                       "col": int(m["col"]), "code": m["code"],
                       "message": m["msg"]})

    passed = r.returncode == 0
    summary = ("Compiled OK." if passed
               else f"{len(errors)} compile error(s).")
    return {"passed": passed, "returncode": r.returncode,
            "file_count": len(sources), "errors": errors[:100],
            "warning_count": warnings, "summary": summary}


# ── HTTP server ──────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._json(200, {"status": "ok"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/compile":
            self._json(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
            project_dir = req["project_dir"]
        except (ValueError, KeyError, json.JSONDecodeError) as e:
            self._json(400, {"error": f"bad request: {e}"})
            return
        self._json(200, compile_project(project_dir))

    def log_message(self, *args) -> None:  # silence default stderr logging
        pass


def _serve() -> None:
    port = int(os.environ.get("PORT", "8080"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    print(f"unity_compile serving on :{port} "
          f"({len(_REFS)} refs)", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--serve":
        _serve()
    elif len(sys.argv) >= 2:
        report = compile_project(sys.argv[1])
        print(json.dumps(report, indent=2))
        sys.exit(0 if report["passed"] else 1)
    else:
        print("usage: unity_compile.py <project_dir> | --serve", file=sys.stderr)
        sys.exit(2)
