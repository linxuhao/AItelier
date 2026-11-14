"""The Godot sidecar's render backend: GPU through a private weston, or CPU.

`_run(render=True)` picks a backend per invocation (GODOT_RENDER_BACKEND =
auto | gpu | cpu). These tests drive the REAL launcher, the real process
session and the real teardown with stand-in programs: a fake `weston` that
binds the Wayland socket it was told to (and starts a helper child, as the real
one starts its shell client), a fake `godot` that prints the GL device line the
real engine prints, a fake `xvfb-run` for the CPU path, and a stand-in render
node. What they pin:

  * selection: cpu is today's command byte for byte; auto uses the GPU when a
    render node opens and weston comes up, otherwise the unchanged CPU command
    with the reason recorded; gpu fails with a clear error instead of falling
    back;
  * once Godot was launched on the GPU there is no second run on CPU;
  * the weston/godot command lines (size follows RENDER_RES, vsync off, a very
    high refresh rate, the Wayland driver);
  * teardown: no weston, helper child, godot or runtime dir survives a pass —
    after success, after a failing engine and after a timeout — and concurrent
    passes never share a runtime dir or socket;
  * every result names the backend that ran (`none` when Godot never started)
    and the GL device Godot printed; the fallback is charged what weston spent;
  * a weston that comes up on Mesa's software rasteriser is not the GPU;
  * /health reports the configured mode and whether weston really comes up
    on the GPU, not only whether a render node opens.
"""
import importlib.util
import json
import os
import subprocess
import tempfile
import textwrap
import threading
import time
from pathlib import Path

import pytest

_HARNESS = Path(__file__).resolve().parents[2] / "docker" / "godot" / "godot_harness.py"
_spec = importlib.util.spec_from_file_location("godot_harness_render_backend", _HARNESS)
gh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gh)

GPU_DEVICE = "FakeVendor - Fake GPU 9000 (fakesi)"
CPU_DEVICE = "Mesa - llvmpipe (LLVM 19, 256 bits)"

FAKE_WESTON = r'''#!/usr/bin/env python3
import json, os, signal, socket, subprocess, sys, time
mode = os.environ.get("FAKE_WESTON_MODE", "ok")
sock = [a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--socket=")][0]
helper = subprocess.Popen(["sleep", "300"])
with open(os.environ["FAKE_WESTON_LOG"], "a") as f:
    f.write(json.dumps({"pid": os.getpid(), "helper": helper.pid, "argv": sys.argv[1:],
                        "runtime": os.environ.get("XDG_RUNTIME_DIR"), "sock": sock}) + "\n")
if mode == "die":
    print("fake weston: no usable GL device", file=sys.stderr)
    helper.kill()
    sys.exit(3)
if mode == "software":
    # Mesa's fallback when no driver for the node loads: weston still comes up.
    print("GL renderer: llvmpipe (LLVM 19.1.7, 256 bits)", file=sys.stderr, flush=True)
elif mode == "ok":
    print("GL renderer: FakeVendor Fake GPU 9000", file=sys.stderr, flush=True)
if mode in ("ok", "software"):
    s = socket.socket(socket.AF_UNIX)
    s.bind(os.path.join(os.environ["XDG_RUNTIME_DIR"], sock))
    s.listen(1)
def stop(*_):
    helper.terminate()
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
while True:
    time.sleep(1)
'''

FAKE_GODOT = r'''#!/usr/bin/env python3
import json, os, sys, time
with open(os.environ["FAKE_GODOT_LOG"], "a") as f:
    f.write(json.dumps({"pid": os.getpid(), "argv": sys.argv[1:],
                        "env": {k: os.environ.get(k) for k in (
                            "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DISPLAY",
                            "LIBGL_ALWAYS_SOFTWARE")}}) + "\n")
if "--headless" not in sys.argv:
    print("Godot Engine v4.x - https://godotengine.org")
    print("OpenGL API 4.6 (Core Profile) - Compatibility - Using Device: "
          + os.environ.get("FAKE_DEVICE", "FakeVendor - Fake GPU 9000 (fakesi)"), flush=True)
out = os.environ.get("AITELIER_PROBE_OUT")
if out:
    with open(out, "w") as f:
        json.dump({"frames": 1, "nodes": {}}, f)
time.sleep(float(os.environ.get("FAKE_GODOT_SLEEP", "0")))
sys.exit(int(os.environ.get("FAKE_GODOT_RC", "0")))
'''

# The CPU path's xvfb-run: records its argv, then runs the rest of the command
# line (the fake godot) with the device the software rasteriser would name.
FAKE_XVFB_RUN = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_XVFB_LOG"], "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:],
                        "libgl": os.environ.get("LIBGL_ALWAYS_SOFTWARE")}) + "\n")
argv = sys.argv[1:]
while argv and argv[0].startswith("-"):
    argv = argv[2:] if argv[0] == "-s" else argv[1:]
os.environ["FAKE_DEVICE"] = "Mesa - llvmpipe (LLVM 19, 256 bits)"
os.execv(argv[0], argv)
'''


def _exe(path: Path, body: str) -> str:
    path.write_text(body)
    path.chmod(0o755)
    return str(path)


def _lines(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _alive(pid: int) -> bool:
    """True while `pid` runs; a zombie (killed, not yet reaped) counts as gone."""
    try:
        state = Path("/proc/%d/stat" % pid).read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state not in ("Z", "X")


def _gone(pid: int, within: float = 5.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """Stand-in weston/godot/xvfb-run and render node, wired through the env."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    dri = tmp_path / "dri"
    dri.mkdir()
    (dri / "renderD128").write_text("")
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    r = type("Rig", (), {})()
    r.weston_log = tmp_path / "weston.jsonl"
    r.godot_log = tmp_path / "godot.jsonl"
    r.xvfb_log = tmp_path / "xvfb.jsonl"
    r.dri = dri
    r.scratch = scratch
    r.godot = _exe(bindir / "godot", FAKE_GODOT)
    _exe(bindir / "xvfb-run", FAKE_XVFB_RUN)
    r.weston = _exe(bindir / "weston", FAKE_WESTON)
    monkeypatch.setenv("PATH", "%s:%s" % (bindir, os.environ.get("PATH", "")))
    monkeypatch.setenv("FAKE_WESTON_LOG", str(r.weston_log))
    monkeypatch.setenv("FAKE_GODOT_LOG", str(r.godot_log))
    monkeypatch.setenv("FAKE_XVFB_LOG", str(r.xvfb_log))
    monkeypatch.setenv("GODOT_WESTON_BIN", r.weston)
    monkeypatch.setenv("GODOT_DRI_DIR", str(dri))
    monkeypatch.delenv("GODOT_RENDER_BACKEND", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(gh, "GODOT_BIN", r.godot)
    monkeypatch.setattr(gh, "RENDER_RES", "1280x720")
    monkeypatch.setattr(gh, "WESTON_READY_SEC", 5.0)
    monkeypatch.setattr(gh, "_WESTON_START", None)
    # Every per-invocation runtime dir is made under this one, so a leak shows.
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    return r


def _leftover_runtime_dirs(r) -> list:
    return sorted(p.name for p in r.scratch.iterdir() if p.name.startswith("godot-wl-"))


def _assert_torn_down(r):
    for row in _lines(r.weston_log):
        assert _gone(row["pid"]), "weston %d survived the pass" % row["pid"]
        assert _gone(row["helper"]), "weston's helper %d survived the pass" % row["helper"]
        assert not Path(row["runtime"]).exists(), row["runtime"]
    for row in _lines(r.godot_log):
        assert _gone(row["pid"]), "godot %d survived the pass" % row["pid"]
    assert _leftover_runtime_dirs(r) == []


# ── selection ──────────────────────────────────────────────────────────────

def test_mode_defaults_to_auto_and_refuses_an_unknown_value(monkeypatch):
    monkeypatch.delenv("GODOT_RENDER_BACKEND", raising=False)
    assert gh._render_backend_mode() == "auto"
    for value in ("gpu", "CPU", " auto "):
        monkeypatch.setenv("GODOT_RENDER_BACKEND", value)
        assert gh._render_backend_mode() == value.strip().lower()
    monkeypatch.setenv("GODOT_RENDER_BACKEND", "vulkan")
    with pytest.raises(ValueError, match="GODOT_RENDER_BACKEND"):
        gh._render_backend_mode()
    with pytest.raises(ValueError, match="GODOT_RENDER_BACKEND"):
        gh._run(["--path", "/p"], timeout=5, render=True)


class _FakePopen:
    seen: list = []

    def __init__(self, cmd, **kwargs):
        _FakePopen.seen.append({"cmd": cmd, "env": kwargs["env"],
                                "new_session": kwargs.get("start_new_session")})
        self.pid = 999999
        self.returncode = 0

    def communicate(self, timeout=None):
        _FakePopen.seen[-1]["timeout"] = timeout
        return ("Using Device: %s\n" % CPU_DEVICE, "")


def _todays_cpu_cmd(args):
    return ["xvfb-run", "-a", "-s", "-screen 0 %sx24" % gh.RENDER_RES, gh.GODOT_BIN,
            "--display-driver", "x11", "--rendering-driver", "opengl3", *args]


@pytest.mark.parametrize("mode,dri_has_node", [("cpu", True), ("auto", False)])
def test_cpu_path_is_todays_command_byte_for_byte(rig, monkeypatch, mode, dri_has_node):
    """cpu always, and auto with no render node, run the unchanged command."""
    if not dri_has_node:
        (rig.dri / "renderD128").unlink()
    monkeypatch.setenv("GODOT_RENDER_BACKEND", mode)
    _FakePopen.seen = []
    monkeypatch.setattr(gh.subprocess, "Popen", _FakePopen)
    cp = gh._run(["--path", "/p", "-s", "res://t.gd"], timeout=77, render=True)
    assert len(_FakePopen.seen) == 1
    call = _FakePopen.seen[0]
    assert call["cmd"] == _todays_cpu_cmd(["--path", "/p", "-s", "res://t.gd"])
    assert call["env"]["LIBGL_ALWAYS_SOFTWARE"] == "1"
    assert "WAYLAND_DISPLAY" not in call["env"]
    assert call["timeout"] == 77 and call["new_session"] is True
    assert cp.render_backend["render_backend"] == "cpu"
    assert cp.render_backend["render_backend_mode"] == mode
    assert cp.render_backend["render_gl_device"] == CPU_DEVICE
    if mode == "cpu":
        assert cp.render_backend["render_fallback_reason"] is None
        assert _lines(rig.weston_log) == []
    else:
        assert "no render node" in cp.render_backend["render_fallback_reason"]


def test_headless_compile_path_is_unchanged(rig, monkeypatch):
    monkeypatch.setenv("GODOT_RENDER_BACKEND", "gpu")
    _FakePopen.seen = []
    monkeypatch.setattr(gh.subprocess, "Popen", _FakePopen)
    cp = gh._run(["--path", "/p", "--import"], timeout=9)
    assert _FakePopen.seen[0]["cmd"] == [gh.GODOT_BIN, "--headless", "--path", "/p", "--import"]
    assert not hasattr(cp, "render_backend")


def test_auto_uses_the_gpu_when_weston_comes_up(rig, monkeypatch):
    monkeypatch.setattr(gh, "RENDER_RES", "1920x1080")
    cp = gh._run(["--path", "/p", "-s", "res://t.gd"], timeout=30, render=True)
    assert cp.returncode == 0
    assert cp.render_backend == {"render_backend": "gpu", "render_backend_mode": "auto",
                                 "render_fallback_reason": None,
                                 "render_gl_device": GPU_DEVICE}
    (weston,) = _lines(rig.weston_log)
    assert "--backend=headless" in weston["argv"] and "--renderer=gl" in weston["argv"]
    assert "--width=1920" in weston["argv"] and "--height=1080" in weston["argv"]
    assert "--refresh-rate=%d" % gh.WESTON_REFRESH_MHZ in weston["argv"]
    assert gh.WESTON_REFRESH_MHZ >= 1000000
    (godot,) = _lines(rig.godot_log)
    assert godot["argv"] == ["--display-driver", "wayland", "--rendering-driver", "opengl3",
                             "--disable-vsync", "--path", "/p", "-s", "res://t.gd"]
    assert godot["env"]["WAYLAND_DISPLAY"] == weston["sock"]
    assert godot["env"]["XDG_RUNTIME_DIR"] == weston["runtime"]
    assert godot["env"]["DISPLAY"] is None and godot["env"]["LIBGL_ALWAYS_SOFTWARE"] is None
    assert Path(weston["runtime"]).parent == rig.scratch
    assert _lines(rig.xvfb_log) == []
    _assert_torn_down(rig)


def test_gpu_pass_drops_the_software_gl_and_x11_env(rig, monkeypatch):
    """An operator's DISPLAY / LIBGL_ALWAYS_SOFTWARE must not reach the GPU
    pass: Godot would draw on llvmpipe while the record says gpu."""
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setenv("LIBGL_ALWAYS_SOFTWARE", "1")
    cp = gh._run(["--path", "/p"], timeout=30, render=True,
                 extra_env={"LIBGL_ALWAYS_SOFTWARE": "1"})
    assert cp.render_backend["render_backend"] == "gpu"
    (godot,) = _lines(rig.godot_log)
    assert godot["env"]["DISPLAY"] is None
    assert godot["env"]["LIBGL_ALWAYS_SOFTWARE"] is None
    _assert_torn_down(rig)


def test_the_fallback_is_charged_what_weston_spent(rig, monkeypatch):
    monkeypatch.setenv("FAKE_WESTON_MODE", "hang")
    monkeypatch.setattr(gh, "WESTON_READY_SEC", 1.0)
    spawned = []
    real_spawn = gh._spawn

    def spawn(cmd, env, timeout, idle_window):
        spawned.append((cmd[0], timeout))
        return real_spawn(cmd, env, timeout, idle_window)

    monkeypatch.setattr(gh, "_spawn", spawn)
    cp = gh._run(["--path", "/p"], timeout=30, render=True)
    assert cp.render_backend["render_backend"] == "cpu"
    (gpu_try, cpu_pass) = spawned
    assert gpu_try == ("sh", 30)
    assert cpu_pass[0] == "xvfb-run"
    # weston's ready window (>= 1 s of waiting) came out of the 30 s budget.
    assert 20 < cpu_pass[1] <= 29.0, cpu_pass
    _assert_torn_down(rig)


def test_a_budget_spent_on_weston_is_a_pass_that_never_ran(rig, monkeypatch):
    def slow_weston(*_a, **_k):
        time.sleep(1.2)
        return None, "weston did not publish its socket"

    monkeypatch.setattr(gh, "_run_gpu", slow_weston)
    with pytest.raises(subprocess.TimeoutExpired) as info:
        gh._run(["--path", "/p"], timeout=1, render=True)
    record = info.value.render_backend
    assert record["render_backend"] == "none"
    assert "weston did not publish" in record["render_fallback_reason"]
    assert "never launched" in record["render_fallback_reason"]
    assert _lines(rig.godot_log) == [] and _lines(rig.xvfb_log) == []


def test_a_timeout_while_weston_starts_is_not_a_gpu_pass(rig, monkeypatch):
    monkeypatch.setenv("FAKE_WESTON_MODE", "hang")
    with pytest.raises(subprocess.TimeoutExpired) as info:
        gh._run(["--path", "/p"], timeout=1, render=True)
    record = info.value.render_backend
    assert record["render_backend"] == "none"
    assert "godot was never launched" in record["render_fallback_reason"]
    assert _lines(rig.godot_log) == [] and _lines(rig.xvfb_log) == []
    _assert_torn_down(rig)


def test_entries_that_never_ran_still_name_their_backend(rig, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_WESTON_MODE", "hang")
    report = gh.run_script(str(_project(tmp_path)), ["res://tests/test_a.gd"],
                           timeout=1, render=True)
    (entry,) = report["results"]
    assert entry["returncode"] == 124
    assert entry["render_backend"] == "none"
    assert report["render_backend"] == "none"
    assert report["render_backend_counts"] == {"none": 1}
    _assert_torn_down(rig)


@pytest.mark.parametrize("weston_mode,reason", [
    ("die", "weston exited"),
    ("hang", "did not publish its socket"),
    ("software", "software GL (llvmpipe"),
])
def test_auto_falls_back_to_cpu_before_godot_starts(rig, monkeypatch, weston_mode, reason):
    monkeypatch.setenv("FAKE_WESTON_MODE", weston_mode)
    monkeypatch.setattr(gh, "WESTON_READY_SEC", 0.5)
    cp = gh._run(["--path", "/p"], timeout=30, render=True)
    assert cp.render_backend["render_backend"] == "cpu"
    assert reason in cp.render_backend["render_fallback_reason"]
    assert cp.render_backend["render_gl_device"] == CPU_DEVICE
    # Godot ran exactly once, and on the CPU path's unchanged command line.
    (godot,) = _lines(rig.godot_log)
    assert godot["argv"] == ["--display-driver", "x11", "--rendering-driver", "opengl3",
                             "--path", "/p"]
    (xvfb,) = _lines(rig.xvfb_log)
    assert xvfb["argv"] == _todays_cpu_cmd(["--path", "/p"])[1:]
    assert xvfb["libgl"] == "1"
    _assert_torn_down(rig)


def test_auto_reports_a_render_node_it_cannot_open(rig, monkeypatch):
    (rig.dri / "renderD128").unlink()
    (rig.dri / "renderD128").mkdir()          # exists, cannot be opened O_RDWR
    cp = gh._run(["--path", "/p"], timeout=30, render=True)
    assert cp.render_backend["render_backend"] == "cpu"
    assert "no render node is openable" in cp.render_backend["render_fallback_reason"]
    assert _lines(rig.weston_log) == []


@pytest.mark.parametrize("break_it,match", [
    ("no_node", "no render node"),
    ("no_weston", "weston is not installed"),
    ("weston_dies", "weston exited"),
    ("weston_on_software_gl", "software GL"),
])
def test_gpu_mode_fails_clearly_and_never_runs_cpu(rig, monkeypatch, break_it, match):
    monkeypatch.setenv("GODOT_RENDER_BACKEND", "gpu")
    if break_it == "no_node":
        (rig.dri / "renderD128").unlink()
    elif break_it == "no_weston":
        monkeypatch.setenv("GODOT_WESTON_BIN", str(rig.scratch / "absent-weston"))
    elif break_it == "weston_dies":
        monkeypatch.setenv("FAKE_WESTON_MODE", "die")
    else:
        monkeypatch.setenv("FAKE_WESTON_MODE", "software")
    with pytest.raises(gh.GpuRenderUnavailable, match=match) as info:
        gh._run(["--path", "/p"], timeout=30, render=True)
    assert "GODOT_RENDER_BACKEND=gpu" in str(info.value)
    assert info.value.render_backend["render_backend"] == "none"
    assert match in info.value.render_backend["render_fallback_reason"]
    assert _lines(rig.godot_log) == [] and _lines(rig.xvfb_log) == []
    _assert_torn_down(rig)


def test_a_failing_gpu_engine_is_the_result_no_cpu_retry(rig, monkeypatch):
    monkeypatch.setenv("FAKE_GODOT_RC", "3")
    cp = gh._run(["--path", "/p"], timeout=30, render=True)
    assert cp.returncode == 3
    assert cp.render_backend["render_backend"] == "gpu"
    assert len(_lines(rig.godot_log)) == 1 and _lines(rig.xvfb_log) == []
    _assert_torn_down(rig)


def test_a_gpu_timeout_tears_down_weston_godot_and_the_runtime_dir(rig, monkeypatch):
    monkeypatch.setenv("FAKE_GODOT_SLEEP", "60")
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as info:
        gh._run(["--path", "/p"], timeout=3, render=True)
    assert time.monotonic() - t0 < 20
    assert info.value.render_backend["render_backend"] == "gpu"
    assert info.value.render_backend["render_gl_device"] == GPU_DEVICE
    assert _lines(rig.xvfb_log) == []
    _assert_torn_down(rig)


def test_an_idle_window_kill_tears_down_the_gpu_pass_too(rig, monkeypatch):
    monkeypatch.setenv("FAKE_GODOT_SLEEP", "60")
    with pytest.raises(gh.ScriptIdleSilence) as info:
        gh._run(["--path", "/p"], timeout=60, render=True, idle_window=2)
    assert info.value.render_backend["render_backend"] == "gpu"
    _assert_torn_down(rig)


def test_concurrent_gpu_passes_never_share_a_compositor(rig, monkeypatch):
    monkeypatch.setenv("FAKE_GODOT_SLEEP", "1")
    results, errors = [], []

    def one(i):
        try:
            results.append(gh._run(["--path", "/p%d" % i], timeout=30, render=True))
        except Exception as exc:          # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=one, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == []
    assert [cp.render_backend["render_backend"] for cp in results] == ["gpu"] * 3
    westons = _lines(rig.weston_log)
    assert len({w["runtime"] for w in westons}) == 3
    assert len({w["sock"] for w in westons}) == 3
    godots = _lines(rig.godot_log)
    assert len({g["env"]["WAYLAND_DISPLAY"] for g in godots}) == 3
    _assert_torn_down(rig)


# ── what the results and /health say ──────────────────────────────────────

def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / "tests").mkdir(parents=True)
    (proj / "project.godot").write_text('config_version=5\n[application]\nconfig/name="t"\n')
    (proj / "tests" / "test_a.gd").write_text("extends SceneTree\nfunc _init():\n\tquit()\n")
    return proj


def test_script_report_names_the_backend_per_entry_and_overall(rig, tmp_path, monkeypatch):
    monkeypatch.setenv("GODOT_EVIDENCE_ROOT", str(tmp_path / "evidence"))
    report = gh.run_script(str(_project(tmp_path)), ["res://tests/test_a.gd"],
                           timeout=30, render=True, retain_requested=True)
    assert report["passed"] is True, report["summary"]
    for key in ("results", "discovered", "render_mode", "render_requested",
                "idle_window_sec", "summary", "retention"):
        assert key in report
    assert report["render_mode"] == "render"
    assert report["render_backend"] == "gpu"
    assert report["render_backend_mode"] == "auto"
    assert report["render_backend_counts"] == {"gpu": 1}
    assert report["render_gl_device"] == GPU_DEVICE
    assert report["render_fallback_reason"] is None
    (entry,) = report["results"]
    assert entry["render_backend"] == "gpu" and entry["render_gl_device"] == GPU_DEVICE
    assert entry["passed"] is True and entry["returncode"] == 0
    manifest = json.loads(Path(report["retention"]["manifest"]).read_text())
    rendered = [row for row in manifest["raw_logs"] if row.get("render_backend")]
    assert rendered and {row["render_backend"] for row in rendered} == {"gpu"}
    assert {row["render_gl_device"] for row in rendered} == {GPU_DEVICE}
    _assert_torn_down(rig)


def test_script_report_names_the_fallback_reason(rig, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_WESTON_MODE", "die")
    report = gh.run_script(str(_project(tmp_path)), ["res://tests/test_a.gd"],
                           timeout=30, render=True)
    assert report["render_backend"] == "cpu"
    assert "weston exited" in report["render_fallback_reason"]
    assert report["render_gl_device"] == CPU_DEVICE
    assert report["results"][0]["render_fallback_reason"] == report["render_fallback_reason"]


def test_headless_script_report_gains_no_backend_fields(rig, tmp_path):
    report = gh.run_script(str(_project(tmp_path)), ["res://tests/test_a.gd"], timeout=30)
    assert "render_backend" not in report
    assert "render_backend" not in report["results"][0]


def test_playtest_probe_and_raw_evidence_name_the_backend(rig, tmp_path):
    raw = []
    probe, errs, timed_out = gh._probe_once(
        ["--path", "/p"], {"AITELIER_PROBE_OUT": str(tmp_path / "state.json")},
        tmp_path / "state.json", 30, True, raw=raw, raw_label="scenario:x")
    assert not timed_out and probe["frames"] == 1
    assert probe["render_backend"] == "gpu" and probe["render_gl_device"] == GPU_DEVICE
    assert raw[0]["render_backend"] == "gpu" and raw[0]["returncode"] == 0
    assert raw[0]["render_gl_device"] == GPU_DEVICE


def test_report_builders_summarise_every_render_pass(rig, monkeypatch):
    assert gh.playtest_project.__wrapped__.__name__ == "playtest_project"
    assert gh.run_script.__wrapped__.__name__ == "run_script"

    def body():
        gh._run(["--path", "/a"], timeout=30, render=True)
        monkeypatch.setenv("GODOT_RENDER_BACKEND", "cpu")
        gh._run(["--path", "/b"], timeout=30, render=True)
        return {"passed": True}

    report = gh._with_render_backend_summary(body)()
    assert report["passed"] is True
    assert report["render_backend"] == "mixed"
    assert report["render_backend_counts"] == {"gpu": 1, "cpu": 1}
    assert report["render_gl_devices"] == sorted([GPU_DEVICE, CPU_DEVICE])
    assert gh._with_render_backend_summary(lambda: {"x": 1})() == {"x": 1}


def test_health_reports_gpu_availability_and_mode(rig, monkeypatch):
    health = gh.health_snapshot()
    for key in ("ok", "engine", "bin", "render_lock", "source_identity", "retention_limits"):
        assert key in health
    assert health["render_backend"]["mode"] == "auto"
    assert health["render_backend"]["gpu_available"] is True
    assert health["render_backend"]["render_node"] == str(rig.dri / "renderD128")
    assert health["render_backend"]["weston_start"] == {"ok": True, "reason": None,
                                                        "source": "probe"}
    # The probe ran once, as a render pass would, and left nothing behind.
    assert len(_lines(rig.weston_log)) == 1
    gh.health_snapshot()
    assert len(_lines(rig.weston_log)) == 1
    assert _lines(rig.godot_log) == []
    _assert_torn_down(rig)
    monkeypatch.setenv("GODOT_RENDER_BACKEND", "cpu")
    (rig.dri / "renderD128").unlink()
    health = gh.health_snapshot()["render_backend"]
    assert health["mode"] == "cpu" and health["gpu_available"] is False
    assert "no render node" in health["gpu_unavailable_reason"]
    monkeypatch.setenv("GODOT_RENDER_BACKEND", "bogus")
    assert "mode_error" in gh.health_snapshot()["render_backend"]


@pytest.mark.parametrize("weston_mode,reason", [
    ("die", "weston exited"),
    ("software", "software GL"),
])
def test_health_says_unavailable_when_weston_cannot_start(rig, monkeypatch,
                                                          weston_mode, reason):
    """A render node that opens but cannot carry weston on the GPU (no working
    driver for it in the image: weston dies, or Mesa falls back to llvmpipe)
    is not an available GPU."""
    monkeypatch.setenv("FAKE_WESTON_MODE", weston_mode)
    monkeypatch.setattr(gh, "WESTON_READY_SEC", 0.5)
    health = gh.health_snapshot()["render_backend"]
    assert health["render_node"] == str(rig.dri / "renderD128")
    assert health["gpu_available"] is False
    assert reason in health["gpu_unavailable_reason"]
    assert health["weston_start"]["source"] == "probe"
    _assert_torn_down(rig)
    # A real pass that brings weston up corrects it; one that cannot, keeps it.
    monkeypatch.setenv("FAKE_WESTON_MODE", "ok")
    gh._run(["--path", "/p"], timeout=30, render=True)
    health = gh.health_snapshot()["render_backend"]
    assert health["gpu_available"] is True
    assert health["weston_start"]["source"] == "render_pass"
    monkeypatch.setenv("FAKE_WESTON_MODE", "die")
    gh._run(["--path", "/p"], timeout=30, render=True)
    health = gh.health_snapshot()["render_backend"]
    assert health["gpu_available"] is False
    assert health["weston_start"] == {"ok": False, "source": "render_pass",
                                      "reason": health["weston_start"]["reason"]}
    assert "weston exited" in health["gpu_unavailable_reason"]
    _assert_torn_down(rig)


def test_gl_device_is_read_from_the_engine_banner():
    line = ("OpenGL API 4.6 (Core Profile) Mesa 25.0.7 - Compatibility - Using Device: "
            "AMD - AMD Radeon Graphics (radeonsi, phoenix)\n")
    assert gh._gl_device("", line) == "AMD - AMD Radeon Graphics (radeonsi, phoenix)"
    assert gh._gl_device(line.encode()) == "AMD - AMD Radeon Graphics (radeonsi, phoenix)"
    assert gh._gl_device("no banner", None) is None


# ── the deployment wiring ─────────────────────────────────────────────────

_ROOT = Path(__file__).resolve().parents[2]


def test_compose_maps_the_render_node_without_a_devices_entry():
    """A `devices:` path missing on the host fails container creation; a bind of
    /dev/dri (created empty by rootful Docker when missing, or pointed at an
    empty directory with AITELIER_DRI_DIR) plus a cgroup rule degrades to CPU."""
    import yaml
    service = yaml.safe_load((_ROOT / "docker-compose.yml").read_text())["services"]["godot-builder"]
    assert "devices" not in service
    assert "${AITELIER_DRI_DIR:-/dev/dri}:/dev/dri" in service["volumes"]
    assert any(rule.startswith("c 226:") for rule in service["device_cgroup_rules"])
    raw = (_ROOT / "docker-compose.yml").read_text()
    assert '"${AITELIER_RENDER_GID:-' in raw
    assert service["group_add"] == ["${AITELIER_RENDER_GID:-993}"]
    assert service["environment"]["GODOT_RENDER_BACKEND"] == "${GODOT_RENDER_BACKEND:-auto}"
    example = (_ROOT / ".env.example").read_text()
    for name in ("AITELIER_RENDER_GID", "GODOT_RENDER_BACKEND", "AITELIER_DRI_DIR"):
        assert name in example, name


def test_image_carries_the_gpu_backend_and_keeps_the_x11_path():
    dockerfile = (_ROOT / "Dockerfile.godot").read_text()
    for package in ("weston", "libegl-mesa0", "libgles2", "libwayland-egl1",
                    "xvfb", "libgl1-mesa-dri", "xdotool"):
        assert package in dockerfile.split(), package
