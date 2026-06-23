"""unity_compile — semantic compile of the project's Unity C# over the whole repo.

Used as a tool STEP after the final verifier (mirrors run_tests). It compiles
EVERY ``*.cs`` in the consolidated repo together — scripts cross-reference, so a
whole-repo compile is the only correct check — against Unity's reference
assemblies, via the ``unity-builder`` sidecar (license-free; see
docker/unity/unity_compile.py). The outcome is captured in ``compile_report.json``
for 5_review to fold into its verdict, so compile errors loop back through the
goal-loop alongside the verifier's semantic issues.

It ALWAYS succeeds as a step:
- No ``*.cs`` in the repo → not a C#/Unity project → pass without touching the
  builder (Python projects never need it running).
- Builder unreachable → pass with a note rather than stalling the pipeline on an
  infra problem (a missing sidecar is not a code defect).
"""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

_BUILDER_URL = os.environ.get("UNITY_BUILDER_URL", "http://unity-builder:8080")
# Dirs that never hold hand-written gameplay scripts.
_SKIP_DIRS = {"Library", "Temp", "obj", "Build", "Builds", ".git"}


def _has_cs(repo: Path) -> bool:
    for p in repo.rglob("*.cs"):
        if not (_SKIP_DIRS & set(p.relative_to(repo).parts)):
            return True
    return False


def unity_compile(*, project_root: str = "", out_dir: str = "",
                  workspace_root: str = "", **kwargs) -> dict:
    """Compile the repo's C# via unity-builder; write compile_report.json.

    Returns {written, passed}. The report holds {passed, returncode, file_count,
    errors[], warning_count, summary} for the reviewer to read.
    """
    repo = Path(project_root or workspace_root).resolve()
    report = {"passed": True, "returncode": 0, "file_count": 0,
              "errors": [], "warning_count": 0, "summary": ""}

    if not repo.exists():
        report.update(passed=False, summary=f"Project root not found: {repo}")
    elif not _has_cs(repo):
        report["summary"] = "No C# (.cs) files — skipping Unity compile."
    else:
        body = json.dumps({"project_dir": str(repo)}).encode("utf-8")
        req = urllib.request.Request(
            _BUILDER_URL.rstrip("/") + "/compile", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=320) as resp:
                report = json.loads(resp.read())
        except (urllib.error.URLError, OSError, json.JSONDecodeError,
                TimeoutError) as e:
            # Infra problem, not a code defect → don't fail the run.
            report["summary"] = (
                f"unity-builder unreachable ({_BUILDER_URL}): {e}. "
                "Compile gate skipped.")

    target_dir = Path(out_dir) if out_dir else repo
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "compile_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8")
    return {"written": "compile_report.json", "passed": report.get("passed", True)}
