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
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ── Unity toolchain paths (verified for ubuntu-6000.0.76f1-base) ──────────
UNITY = Path("/opt/unity/Editor/Data")
# Editor binary — used by /playtest to run PlayMode tests (needs a license, unlike
# the compiler). EDITOR_VERSION must track the Dockerfile.unity FROM tag: the
# play-test copies the project to a throwaway dir and pins ProjectVersion to this
# so the editor opens it as its own (no newer-project up/downgrade block).
EDITOR = UNITY.parent / "Unity"
EDITOR_VERSION = "6000.0.76f1"
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


# ── PlayMode smoke test (license-gated; runs the editor) ─────────────────
# The generated project has no .asmdef (scripts live in Assembly-CSharp) and an
# .asmdef-scoped test assembly CANNOT reference Assembly-CSharp — so the smoke
# test finds SceneBootstrapper by NAME via reflection and drives the project's
# own BuildScene() convention. It asserts two things the compile gate can't:
#   1. No Exception/Error logged on the first frames (catches the legacy-Input
#      InvalidOperationException + first-frame NREs).
#   2. BuildScene() produced >1 MonoBehaviour (catches an empty/orphaned scene).

_SMOKE_ASMDEF = json.dumps({
    "name": "AItelier.PlayTests",
    "references": ["UnityEngine.TestRunner", "UnityEditor.TestRunner"],
    "includePlatforms": [],
    "excludePlatforms": [],
    "precompiledReferences": ["nunit.framework.dll"],
    "autoReferenced": False,
    "defineConstraints": ["UNITY_INCLUDE_TESTS"],
    "optionalUnityReferences": ["TestAssemblies"],
    "overrideReferences": True,
}, indent=2)

_SMOKE_TEST = r'''using System;
using System.Collections;
using System.Collections.Generic;
using System.Linq;
using NUnit.Framework;
using UnityEngine;
using UnityEngine.TestTools;

// Injected by AItelier unity-builder /playtest. Do not edit — it is reflection-
// based on purpose so it works against any generated game (scripts are in the
// default Assembly-CSharp, which an .asmdef test cannot reference directly).
public class AItelierSmokeTest
{
    [UnityTest]
    public IEnumerator Scene_Builds_And_Runs_Without_Errors()
    {
        var errors = new List<string>();
        Application.LogCallback handler = (msg, trace, type) =>
        {
            if (type == LogType.Exception || type == LogType.Error)
                errors.Add(type + ": " + msg);
        };
        Application.logMessageReceived += handler;
        try
        {
            Type boot = AppDomain.CurrentDomain.GetAssemblies()
                .SelectMany(a => { try { return a.GetTypes(); } catch { return new Type[0]; } })
                .FirstOrDefault(t => t.Name == "SceneBootstrapper"
                                     && typeof(MonoBehaviour).IsAssignableFrom(t));
            Assert.IsNotNull(boot, "No SceneBootstrapper MonoBehaviour found in any loaded assembly.");

            var go = new GameObject("AItelierSmoke");
            go.AddComponent(boot);   // Awake() should call BuildScene()
            yield return null;
            yield return null;

            // Fallback: some bootstrappers expose BuildScene() but don't auto-run it.
            var mbs = UnityEngine.Object.FindObjectsByType<MonoBehaviour>(FindObjectsSortMode.None);
            if (mbs.Length <= 1)
            {
                var m = boot.GetMethod("BuildScene");
                if (m != null) m.Invoke(go.GetComponent(boot), null);
                yield return null;
                yield return null;
                mbs = UnityEngine.Object.FindObjectsByType<MonoBehaviour>(FindObjectsSortMode.None);
            }

            Assert.IsEmpty(errors,
                "Runtime errors during scene build/run:\n" + string.Join("\n", errors));
            Assert.Greater(mbs.Length, 1,
                "BuildScene produced no gameplay objects — scene is empty after bootstrap.");
        }
        finally
        {
            Application.logMessageReceived -= handler;
        }
    }
}
'''


def _prepare_playtest_copy(src: Path, proj: Path) -> None:
    """Copy the (read-only) project to a writable dir and make it testable.

    Drops import artifacts, pins ProjectVersion to this editor, removes the
    package lock so the resolver re-binds to versions this editor ships, adds
    com.unity.test-framework, and injects the reflection smoke test.
    """
    shutil.copytree(src, proj, ignore=shutil.ignore_patterns(
        "Library", "Temp", "obj", "Build", "Builds", ".git", "Logs"))

    pv = proj / "ProjectSettings" / "ProjectVersion.txt"
    if pv.exists():
        pv.write_text(f"m_EditorVersion: {EDITOR_VERSION}\n"
                      f"m_EditorVersionWithRevision: {EDITOR_VERSION}\n",
                      encoding="utf-8")

    # Let Package Manager re-resolve against THIS editor's registry.
    (proj / "Packages" / "packages-lock.json").unlink(missing_ok=True)

    mani = proj / "Packages" / "manifest.json"
    try:
        data = json.loads(mani.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {"dependencies": {}}
    data.setdefault("dependencies", {}).setdefault(
        "com.unity.test-framework", "1.4.5")
    mani.parent.mkdir(parents=True, exist_ok=True)
    mani.write_text(json.dumps(data, indent=2), encoding="utf-8")

    tdir = proj / "Assets" / "AItelierPlayTests"
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "AItelier.PlayTests.asmdef").write_text(_SMOKE_ASMDEF, encoding="utf-8")
    (tdir / "AItelierSmokeTest.cs").write_text(_SMOKE_TEST, encoding="utf-8")


def _parse_playtest_results(results: Path, log: Path, returncode: int) -> dict:
    """Parse the NUnit3 results.xml the Test Runner writes; fall back to the log."""
    if not results.exists():
        tail = ""
        if log.exists():
            tail = "\n".join(log.read_text(errors="replace").splitlines()[-40:])
        return {"passed": False, "total": 0, "passed_count": 0, "failed_count": 0,
                "failures": [],
                "summary": f"No test results produced (editor rc={returncode}). "
                           f"Log tail:\n{tail}"}
    try:
        root = ET.parse(results).getroot()
    except ET.ParseError as e:
        return {"passed": False, "total": 0, "passed_count": 0, "failed_count": 0,
                "failures": [], "summary": f"Unparseable results.xml: {e}"}

    total = int(root.get("total", 0) or 0)
    passed_n = int(root.get("passed", 0) or 0)
    failed_n = int(root.get("failed", 0) or 0)
    failures = []
    for tc in root.iter("test-case"):
        if tc.get("result") == "Failed":
            msg = tc.find("./failure/message")
            failures.append({
                "name": tc.get("fullname") or tc.get("name"),
                "message": (msg.text or "").strip() if msg is not None else "",
            })
    # 0 tests discovered ⇒ the harness/import failed, not a real pass.
    passed = total > 0 and failed_n == 0
    summary = (f"PlayMode: {passed_n}/{total} passed."
               if total else "No PlayMode tests were discovered/run.")
    return {"passed": passed, "total": total, "passed_count": passed_n,
            "failed_count": failed_n, "failures": failures[:50], "summary": summary}


# The entrypoint touches this marker iff online activation (UNITY_EMAIL/PASSWORD)
# succeeded. Running the editor needs a license, so with no marker the play-test
# SKIPS (passed=True) rather than hard-failing — an unlicensed builder is an infra
# state, not a code defect.
_LICENSE_MARKER = Path(os.environ.get("HOME", "/tmp/unity-home")) / ".aitelier_licensed"


def _licensed() -> bool:
    return _LICENSE_MARKER.exists()


def _skip(summary: str) -> dict:
    return {"passed": True, "total": 0, "passed_count": 0, "failed_count": 0,
            "failures": [], "summary": summary}


def playtest_project(project_dir: str) -> dict:
    """Run the injected PlayMode smoke test against a copy of project_dir.

    Returns {passed, total, passed_count, failed_count, failures[], summary}.
    Skips (passed=True) for non-Unity projects, a missing editor, or no license.
    """
    src = Path(project_dir).resolve()
    if not (src / "Assets").is_dir():
        return _skip("No Assets/ — not a Unity project; play-test skipped.")
    if not EDITOR.exists():
        return _skip(f"Editor binary not found ({EDITOR}); play-test skipped.")
    if not _licensed():
        return _skip("Unity editor not licensed (online activation absent or "
                     "failed); play-test skipped.")

    with tempfile.TemporaryDirectory(prefix="playtest-") as tmp:
        proj = Path(tmp) / "proj"
        _prepare_playtest_copy(src, proj)
        results = proj / "results.xml"
        log = proj / "editor.log"
        cmd = [str(EDITOR), "-runTests", "-batchmode", "-nographics",
               "-projectPath", str(proj), "-testPlatform", "PlayMode",
               "-testResults", str(results), "-logFile", str(log)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        except subprocess.TimeoutExpired:
            return {"passed": False, "total": 0, "passed_count": 0,
                    "failed_count": 0, "failures": [],
                    "summary": "PlayMode tests timed out after 900s "
                               "(first asset import can be slow)."}
        return _parse_playtest_results(results, log, r.returncode)


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
        if self.path not in ("/compile", "/playtest"):
            self._json(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
            project_dir = req["project_dir"]
        except (ValueError, KeyError, json.JSONDecodeError) as e:
            self._json(400, {"error": f"bad request: {e}"})
            return
        fn = compile_project if self.path == "/compile" else playtest_project
        self._json(200, fn(project_dir))

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
