"""The stale Chromium singleton lock guard in deploy/pwmcp/run-pwmcp.sh.

The marked helper is extracted from the script and EXECUTED with bash against a
fixture proc root and temporary profiles. The branch order is checked with the same
shape as test_run_pwmcp_steward.py (real bash, a recording docker stub), plus a
PWMCP_TIMELINE trace written from inside the helper and an empty fixture proc root.
No docker daemon, network or browser is needed.
"""
from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "pwmcp" / "run-pwmcp.sh"
MARK_BEGIN = "# >>> stale-profile-lock"
MARK_END = "# <<< stale-profile-lock"
LOCKS = ("SingletonLock", "SingletonCookie", "SingletonSocket")
FUNCTION_START = "clear_stale_profile_lock() {\n"


def helper_text():
    """The exact marked function, so a rename or a lost marker cannot pass vacuously."""
    text = SCRIPT.read_text()
    assert MARK_BEGIN in text and MARK_END in text, "both marker lines must be present"
    body = text.split(MARK_BEGIN, 1)[1].split(MARK_END, 1)[0]
    assert body.strip(), "the marked helper must not be empty"
    assert FUNCTION_START in body, "the helper must define clear_stale_profile_lock"
    return body


def run_helper(root, profile, proc_root):
    """Execute the extracted helper under set -euo pipefail, with a fixture /proc."""
    script = root / "helper-under-test.sh"
    script.write_text("set -euo pipefail\n" + helper_text()
                      + '\nclear_stale_profile_lock "$1"\n')
    env = {**os.environ, "PWMCP_PROC_ROOT": str(proc_root)}
    return subprocess.run(["bash", str(script), str(profile)], env=env,
                          capture_output=True, text=True, timeout=30)


def locked_profile(root, name="profile"):
    profile = root / name
    profile.mkdir()
    # The lock is a dangling symlink in real profiles: it names the old host and pid.
    for lock in LOCKS:
        (profile / lock).symlink_to("old-host-4321")
    return profile


def proc_root(root, entries, name="proc"):
    """entries: {pid: argv}; cmdline is NUL-separated as in the real process table."""
    base = root / name
    base.mkdir(parents=True, exist_ok=True)
    for pid, argv in entries.items():
        entry = base / str(pid)
        entry.mkdir(parents=True, exist_ok=True)
        (entry / "cmdline").write_bytes(b"".join(arg.encode() + b"\0" for arg in argv))
    return base


# ── the helper, actually executed ─────────────────────────────────────────────


def test_a_stale_lock_is_cleared_when_no_process_names_the_profile(tmp_path):
    profile = locked_profile(tmp_path)
    unrelated = profile / "History"
    unrelated.write_text("keep this\n")
    other = tmp_path / "other-profile"
    # An unreadable entry and a non-numeric entry must not abort the script either.
    root = proc_root(tmp_path, {
        4242: ["/usr/bin/chromium", f"--user-data-dir={other}"],
        7777: ["/usr/bin/chromium", f"--user-data-dir={other}-suffix"],
        "self": ["bash", f"--user-data-dir={profile}"],
    })
    unreadable = proc_root(tmp_path, {}, "unreadable")
    (unreadable / "8888").mkdir()
    (unreadable / "8888" / "cmdline").write_bytes(b"chromium\0")
    (unreadable / "8888" / "cmdline").chmod(0)

    done = run_helper(tmp_path, profile, root)
    assert done.returncode == 0, done.stderr
    assert all(not os.path.lexists(profile / lock) for lock in LOCKS)
    assert unrelated.read_text() == "keep this\n"
    assert str(profile) in done.stdout
    assert all(lock in done.stdout for lock in LOCKS), done.stdout

    # A cmdline that cannot be read is skipped, not fatal under set -e, and leaves no
    # error on stderr: on a real recreate /proc entries do vanish between the glob and the
    # open, and 4 such lines appeared on stderr before stderr was redirected first.
    profile2 = locked_profile(tmp_path, "profile2")
    done2 = run_helper(tmp_path, profile2, unreadable)
    assert done2.returncode == 0, done2.stderr
    assert done2.stderr == "", done2.stderr
    assert all(not os.path.lexists(profile2 / lock) for lock in LOCKS)

    # Same for an entry whose cmdline file is absent altogether (a process that vanished).
    absent = proc_root(tmp_path, {}, "absent")
    (absent / "9999").mkdir()
    profile3 = locked_profile(tmp_path, "profile3")
    done3 = run_helper(tmp_path, profile3, absent)
    assert done3.returncode == 0, done3.stderr
    assert done3.stderr == "", done3.stderr
    assert all(not os.path.lexists(profile3 / lock) for lock in LOCKS)


def test_the_lock_is_kept_when_a_live_process_names_this_profile(tmp_path):
    profile = locked_profile(tmp_path)
    root = proc_root(tmp_path, {
        "not-a-pid": ["/usr/bin/chromium", f"--user-data-dir={profile}"],
        31337: ["chromium", "--no-sandbox", f"--user-data-dir={profile}", "--headless"],
    })
    done = run_helper(tmp_path, profile, root)
    assert done.returncode == 0, done.stderr
    assert all(os.path.lexists(profile / lock) for lock in LOCKS)
    assert "31337" in done.stdout
    assert str(profile) in done.stdout
    assert "kept" in done.stdout.lower(), done.stdout


def test_an_absent_lock_is_a_silent_success(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    root = proc_root(tmp_path, {1234: ["/usr/bin/chromium", "--headless"]})
    done = run_helper(tmp_path, profile, root)
    assert done.returncode == 0, done.stderr
    assert done.stdout == ""
    assert sorted(p.name for p in profile.iterdir()) == []


# ── both branches call it between docker rm -f and docker run ─────────────────


def run_rendered(root, driver):
    """Render a branch as test_run_pwmcp_steward.py does: real bash, stub docker.

    A trace line is added at the top of the extracted helper so its call appears in
    the same timeline as the stub's docker calls. Nothing else in the script changes.
    """
    bin_dir = Path(tempfile.mkdtemp(dir=root, prefix="bin-"))
    log, timeline, stub = bin_dir / "docker.log", bin_dir / "timeline.log", bin_dir / "docker"
    stub.write_text(
        r'#!/bin/sh' '\n'
        r'printf "%s\n" "$1 $2 $3" >> "$DOCKER_LOG"' '\n'
        r'printf "docker %s\n" "$1" >> "$PWMCP_TIMELINE"' '\n'
        r'exit 0' '\n'
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)

    rendered = root / "rendered.sh"
    text = SCRIPT.read_text()
    assert text.count(FUNCTION_START) == 1
    traced = text.replace(FUNCTION_START,
                          FUNCTION_START + r'  printf "clear_stale_profile_lock\n" >> "$PWMCP_TIMELINE"' + "\n",
                          1)
    rendered.write_text(traced)

    home = root / "home"
    home.mkdir(exist_ok=True)
    dri = root / "empty-dri"
    dri.mkdir(exist_ok=True)
    empty_proc = root / "empty-proc"
    empty_proc.mkdir(exist_ok=True)
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(home),
           "DOCKER_LOG": str(log), "PWMCP_TIMELINE": str(timeline),
           "PWMCP_BROWSER_DIR": str(root / "browser"), "PWMCP_DRI_DIR": str(dri),
           "PWMCP_PROC_ROOT": str(empty_proc)}
    done = subprocess.run(["bash", str(rendered), driver, "8932"], env=env,
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    assert log.is_file()
    return timeline.read_text().splitlines()


def test_the_steward_branch_clears_between_rm_and_run(tmp_path):
    order = run_rendered(tmp_path, "steward")
    assert order.count("docker rm") == 1, order
    assert order.count("clear_stale_profile_lock") == 1, order
    assert order.count("docker run") == 1, order
    assert order.index("docker rm") < order.index("clear_stale_profile_lock") < order.index("docker run")


def test_the_driver_branch_clears_between_rm_and_run(tmp_path):
    order = run_rendered(tmp_path, "grok")
    assert order.count("docker rm") == 1, order
    assert order.count("clear_stale_profile_lock") == 1, order
    assert order.count("docker run") == 1, order
    assert order.index("docker rm") < order.index("clear_stale_profile_lock") < order.index("docker run")
