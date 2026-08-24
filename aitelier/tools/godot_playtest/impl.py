"""godot_playtest — headless play-test of the project's Godot scene.

Used as a tool STEP after the compile gate (mirrors run_tests / godot_compile).
It POSTs the consolidated repo path to the ``godot-builder`` sidecar's
``/playtest`` route, which copies the project, injects an autoload probe, runs
the main scene headless for a bounded number of frames (auto-pressing an input
action so the game progresses), and reports:
  * every runtime error (SCRIPT ERROR / push_error) with a res:// file + line
  * a JSON snapshot of the live scene tree's script variables — the runtime
    state an agent needs to actually SEE what the game is doing.
  * PNGs of real rendered frames, unpacked into ``<out_dir>/frames/``.
The outcome lands in ``playtest_report.json`` for 5_review to fold into its
verdict, so runtime failures loop back through the goal-loop alongside parse
errors.

It ALWAYS succeeds as a step:
- No ``project.godot`` → not a Godot project → pass without touching the builder.
- Builder unreachable → pass with a LOUD ``gate_skipped`` note rather than
  stalling on infra (a missing sidecar is not a code defect — but the scene
  shipped without a runtime smoke test, so 5_review must see it).
"""

import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

_BUILDER_URL = os.environ.get("GODOT_BUILDER_URL", "http://godot-builder:8080")

SPEC_DIR = "playtest"
SPEC_FILE = "playtest_spec.yaml"


# ── Reading the authored contract ─────────────────────────────────────────
# TWO SHAPES, ONE READER.
#   playtest/            one file per scenario + a `_common.yaml` carrying the
#                        shared scene/actions/surface and a `scenario_order`.
#   playtest_spec.yaml   the original monolith, everything in one document.
# The directory is preferred when it yields scenarios; the monolith is the
# fallback, because every project that has not split still carries it.
#
# WHY THE SPLIT. jinyong-assets' contract reached 26 scenarios in a 1478-line
# file, so every single-scenario repair was a rewrite of all 26 — and a rewrite
# of all 26 is how assertions disappear. The 2026-08-24 audit of the previous
# round compared scenario→assert counts across the round and found scenarios
# that came back from a "fix" with fewer assertions than they went in with; the
# diff was the width of the file, so review never saw it.
#
# WHY LOADING IS STRICT. A scenario file the reader cannot parse is reported as
# a HARD failure, never skipped. A play-test that quietly evaluates 25 of 26
# scenarios and reports "all assertions passed" is a green light over an
# absence — the very defect the split exists to remove.


def _load_yaml(path: Path):
    import yaml
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _read_monolith(repo: Path) -> dict | None:
    """The single-file contract at the repo root. Best-effort: a malformed
    monolith degrades to the legacy canned smoke test, which is the behaviour
    every pre-split project already had."""
    p = repo / SPEC_FILE
    if not p.is_file():
        return None
    try:
        spec = _load_yaml(p)
    except Exception:
        return None
    return spec if isinstance(spec, dict) and spec.get("scenarios") else None


def _read_split(repo: Path) -> tuple[dict | None, list[str]]:
    """Assemble the contract from ``playtest/``. Returns ``(spec, errors)``.

    ``errors`` holds only the things that would SILENTLY SHRINK the contract —
    a scenario file that will not parse, one with no timeline, a name in
    ``scenario_order`` with no file behind it. They are reported, not skipped.
    """
    d = repo / SPEC_DIR
    if not d.is_dir():
        return None, []

    errors: list[str] = []
    common: dict = {}
    cp = d / "_common.yaml"
    if cp.is_file():
        try:
            loaded = _load_yaml(cp)
        except Exception as e:
            errors.append(f"{SPEC_DIR}/_common.yaml unreadable: {type(e).__name__}: {e}")
            loaded = None
        if isinstance(loaded, dict):
            common = loaded
        elif loaded is not None:
            errors.append(f"{SPEC_DIR}/_common.yaml is not a YAML mapping")

    order = [str(n) for n in (common.pop("scenario_order", None) or [])]

    found: dict[str, dict] = {}
    for p in sorted(d.glob("*.yaml")):
        if p.name.startswith("_"):
            continue          # _common.yaml and any future shared fragment
        try:
            sc = _load_yaml(p)
        except Exception as e:
            errors.append(f"{SPEC_DIR}/{p.name} unreadable: {type(e).__name__}: {e}")
            continue
        if not isinstance(sc, dict) or sc.get("timeline") is None:
            errors.append(
                f"{SPEC_DIR}/{p.name} is not a scenario (no `timeline:`). Every "
                f"*.yaml here must be one scenario; shared fragments take a "
                f"leading underscore.")
            continue
        sc.setdefault("name", p.stem)
        if str(sc["name"]) != p.stem:
            errors.append(f"{SPEC_DIR}/{p.name} declares name {sc['name']!r} — "
                          f"basename and `name:` must match.")
        found[p.stem] = sc

    if not found:
        # An empty directory is not a contract; fall through to the monolith
        # rather than hard-failing a repo that carries an empty playtest/.
        return None, errors

    missing = [n for n in order if n not in found]
    if missing:
        errors.append(f"scenario_order names {missing} have no file in {SPEC_DIR}/.")

    # scenario_order fixes the run order; anything unlisted is appended sorted,
    # so dropping in a new file is enough to have it run.
    names = ([n for n in order if n in found]
             + [n for n in sorted(found) if n not in order])
    spec = dict(common)
    spec["scenarios"] = [found[n] for n in names]
    return spec, errors


def read_spec(repo: Path) -> tuple[dict | None, dict]:
    """Load the play-test contract from ``repo``.

    Returns ``(spec, info)`` where ``info`` is
    ``{"source": "playtest/" | "playtest_spec.yaml" | "", "errors": [...],
    "notes": [...]}``. ``spec`` is None when the repo declares no contract —
    the sidecar then runs the legacy canned smoke test.
    """
    info: dict = {"source": "", "errors": [], "notes": []}
    split, errors = _read_split(repo)
    info["errors"] = errors
    if split is not None:
        info["source"] = SPEC_DIR + "/"
        if (repo / SPEC_FILE).is_file():
            # Both shapes on disk. The directory wins and the monolith is dead
            # weight that will drift — say so where a reviewer reads it, because
            # a stale contract that still looks authoritative is how the next
            # round edits the file nothing reads.
            info["notes"].append(
                f"both {SPEC_DIR}/ and {SPEC_FILE} exist — {SPEC_DIR}/ was used "
                f"and {SPEC_FILE} was IGNORED. Fold it in or delete it.")
        return split, info
    mono = _read_monolith(repo)
    if mono is not None:
        info["source"] = SPEC_FILE
        return mono, info
    return None, info


def select_scenarios(spec: dict, names: list[str]) -> tuple[dict, list[str]]:
    """Narrow a loaded contract to the named scenarios, keeping the shared
    header. Returns ``(spec, unknown)``; ``unknown`` names are NOT silently
    dropped — the caller reports them."""
    by_name = {str(s.get("name")): s for s in (spec.get("scenarios") or [])}
    unknown = [n for n in names if n not in by_name]
    picked = {k: v for k, v in spec.items() if k != "scenarios"}
    picked["scenarios"] = [by_name[n] for n in names if n in by_name]
    return picked, unknown


def _unpack_frames(report: dict, target_dir: Path) -> None:
    """Materialise the sidecar's base64 frame captures under ``<out_dir>/frames/``
    and leave a relative path behind.

    The sidecar mounts the workspace read-only and copies projects to a container-
    local temp dir, so the PNGs cannot be written where they belong — they ride
    home inside the JSON. Unpacking them here is what makes the report readable:
    a reviewer opens frames/..., and playtest_report.json never holds a blob."""
    frames_dir = target_dir / "frames"
    for cap in report.get("captures") or []:
        blob = cap.pop("png_b64", "")
        if not blob:
            continue
        name = os.path.basename(str(cap.get("file") or "frame.png"))
        frames_dir.mkdir(parents=True, exist_ok=True)
        (frames_dir / name).write_bytes(base64.b64decode(blob))
        cap["file"] = f"frames/{name}"


def post_playtest(payload: dict, timeout: int = 1200) -> dict:
    """POST one /playtest request to the sidecar and return its report, or a
    LOUD gate_skipped report if the sidecar cannot be reached.

    1200s, not 420. The play-test scales with the project and this gate does not
    degrade gracefully: on timeout the caller records gate_skipped + passed:true,
    so as a project grows the gate does not get slower, it DISAPPEARS — and it
    disappears as a pass. jinyong-spine, 2026-08-23: 24 scripts/11 scenarios ->
    55/20, and the builder log shows the exception on wfile.write(body) while
    sending the 200 — the run had FINISHED and the answer had nowhere to go.
    """
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _BUILDER_URL.rstrip("/") + "/playtest", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError,
            TimeoutError) as e:
        return {"passed": True, "frames": 0, "errors": [], "state": {},
                "behavior": None, "spec_used": False, "gate_skipped": True,
                "summary": (f"godot-builder unreachable ({_BUILDER_URL}): {e}. "
                            "Play-test gate skipped — scene NOT smoke-tested.")}


def godot_playtest(*, project_root: str = "", out_dir: str = "",
                   workspace_root: str = "", **kwargs) -> dict:
    """Run the headless play-test via godot-builder; write playtest_report.json.

    Reads the authored contract if present — ``playtest/`` (one file per
    scenario) preferred, root ``playtest_spec.yaml`` as the fallback → scenario-
    driven TDD play-test (input timeline + live Expression assertions); else the
    legacy canned smoke test. Returns {written, passed}. The report holds
    {passed (HARD: crash/didn't-run), behavior (ADVISORY per-scenario asserts),
    frames, errors[], state, spec_used, spec_source, summary} for the reviewer.
    """
    repo = Path(project_root or workspace_root).resolve()
    report = {"passed": True, "frames": 0, "errors": [], "state": {},
              "behavior": None, "spec_used": False, "summary": ""}
    info: dict = {"source": "", "errors": [], "notes": []}

    if not repo.exists():
        report.update(passed=False, summary=f"Project root not found: {repo}")
    elif not (repo / "project.godot").is_file():
        report["summary"] = "No project.godot — not a Godot project; play-test skipped."
    else:
        spec, info = read_spec(repo)
        if info["errors"]:
            # The contract could not be read WHOLE. Do not run a subset and
            # report on it: a scenario that vanishes between the repo and the
            # gate is invisible in the results, and `all(passed)` then holds
            # over whatever survived.
            report.update(
                passed=False, spec_used=False,
                spec_load_errors=info["errors"],
                summary=("Playtest HARD-failed: the play-test contract could not "
                         "be read whole — %d problem(s) in the spec: %s"
                         % (len(info["errors"]), " | ".join(info["errors"][:5]))))
        else:
            payload = {"project_dir": str(repo)}
            if spec:
                payload["spec"] = spec
            report = post_playtest(payload)
            # The sidecar says there is no Godot project at a path where THIS
            # process just stat'd project.godot. It is not looking at the same
            # bytes we are — a bind mount whose source directory was replaced
            # keeps resolving to the old, unlinked inode. That is a hard
            # failure, not a skip: on run jinyong-ui it returned passed:true
            # with file_count 0 and captures 0, the reviewer read it as clean,
            # and 21 scripts plus a real failing assertion went unseen for the
            # whole run. gate_skipped does not cover this — the builder was
            # perfectly reachable; it was blind.
            if report.get("no_project"):
                report["passed"] = False
                report["blind_builder"] = True
                report["summary"] = (
                    f"godot-builder cannot see {repo} — it reports no project.godot "
                    f"at a path this process can read. Its workspace mount is stale; "
                    f"recreate the container (a restart is not enough). "
                    f"Play-test gate NOT run.")

    report["spec_source"] = info["source"]
    if info["notes"]:
        # Rides on `summary` because that is the line the play-test summary puts
        # at the top and 5_review actually reads.
        report["summary"] = "WARNING: " + " ".join(info["notes"]) + " || " + str(
            report.get("summary", ""))

    target_dir = Path(out_dir) if out_dir else repo
    target_dir.mkdir(parents=True, exist_ok=True)
    _unpack_frames(report, target_dir)
    (target_dir / "playtest_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8")
    return {"written": "playtest_report.json", "passed": report.get("passed", True)}
