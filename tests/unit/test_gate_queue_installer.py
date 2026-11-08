"""Real subprocess tests for tools/install_gate_queue.py.

Every case drives the actual installer as a child process against a temporary
target root, a temporary git repository and a temporary queue root, so what is
measured is the tool's decision -- never the host's current queue or bin.

The daemon/ticket probe is exercised both ways: against the queue's own real
artifacts in the temporary root (an exclusive flock on `daemon.lock`, a pidfile,
`tickets/*/ticket.json`) including one genuinely running `scripts/gate_queue.py
daemon`, and through a recording stub passed as `--daemon-probe`/`--ticket-probe`
that records the queue directory it was given and exits either way on command.

No app/SDK imports; run only in the prescribed disposable offline CPU capsule.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest


# GATE_QUEUE_INSTALLER_ROOT relocates the tool and the real queue together, so a
# deliberate mutant can be measured by this same file without editing it.
ROOT = Path(os.environ.get("GATE_QUEUE_INSTALLER_ROOT",
                           Path(__file__).resolve().parents[2])).resolve()
TOOL = ROOT / "tools" / "install_gate_queue.py"
QUEUE = ROOT / "scripts" / "gate_queue.py"      # the real queue, for a real daemon

REVIEWED_V1 = b'''#!/usr/bin/env python3
"""reviewed queue, first cut"""
print("v1")
'''
REVIEWED_V2 = b'''#!/usr/bin/env python3
"""reviewed queue, second cut"""
print("v2")
'''
STALE_INSTALLED = b"#!/usr/bin/env python3\nprint('stale installed copy')\n"

STUB = '''#!/usr/bin/env python3
import os, sys
with open({record!r}, "a", encoding="utf-8") as out:
    out.write("%s %s %s\\n" % (os.environ.get("GATE_QUEUE_PROBE", "?"), os.getpid(),
                               "|".join(sys.argv[1:])))
print({line!r})
raise SystemExit({rc!r})
'''


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def snapshot(path):
    if not os.path.lexists(path):
        return None
    s = os.lstat(path)
    raw = b"" if not stat.S_ISREG(s.st_mode) else path.read_bytes()
    return (raw, s.st_ino, stat.S_IMODE(s.st_mode), s.st_uid, s.st_gid,
            s.st_size, s.st_mtime_ns, s.st_ctime_ns)


class GateQueueInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.env = {name: value for name, value in os.environ.items()
                    if name != "GATE_QUEUE_DIR"}
        self.env["HOME"] = str(self.home)

        # A real git repository: the reviewed bytes must come out of Git.
        self.repo = self.root / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        self.git("init", "-q")
        (self.repo / "scripts" / "gate_queue.py").write_bytes(REVIEWED_V1)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "reviewed v1")
        (self.repo / "scripts" / "gate_queue.py").write_bytes(REVIEWED_V2)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "reviewed v2")
        self.commit = self.git("rev-parse", "HEAD").stdout.strip()
        self.reviewed = REVIEWED_V2
        self.sha = digest(self.reviewed)

        self.dest = self.root / "bin"
        self.dest.mkdir()
        self.target = self.dest / "gate_queue.py"
        self.target.write_bytes(STALE_INSTALLED)
        self.target.chmod(0o640)
        self.sibling = self.dest / "unrelated"
        self.sibling.write_bytes(b"unrelated sibling\n")
        self.sibling_before = snapshot(self.sibling)

        self.queue = self.root / "queue"
        (self.queue / "tickets").mkdir(parents=True)
        self.queue_before = self.tree_snapshot(self.queue)

    # --- helpers ---------------------------------------------------------

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Queue installer test",
                               "-c", "user.email=queue@example.invalid", *args],
                              capture_output=True, text=True, timeout=30)

    def tree_snapshot(self, directory):
        return {str(path.relative_to(directory)): snapshot(path)
                for path in sorted(directory.rglob("*"))}

    def tokens(self):
        return ["--commit", self.commit, "--sha256", self.sha,
                "--repo", str(self.repo), "--queue-dir", str(self.queue)]

    def invoke(self, args=None, **kwargs):
        if args is None:
            args = [*self.tokens(), str(self.dest)]
        return subprocess.run([sys.executable, str(TOOL), *args], cwd=self.root,
                              capture_output=True, text=True, timeout=120,
                              env=kwargs.pop("env", self.env), **kwargs)

    def ticket(self, ticket, state, **extra):
        directory = self.queue / "tickets" / ticket
        directory.mkdir(parents=True, exist_ok=True)
        record = dict(ticket=ticket, worktree="/nonexistent", sha="a" * 40, label="",
                      state=state, submitted_at="2026-11-08T00:00:00+00:00",
                      report_dir=None, gate_exit_code=None, verdict=None, error=None)
        record.update(extra)
        (directory / "ticket.json").write_text(json.dumps(record) + "\n", encoding="utf-8")

    def stub(self, name, line, rc):
        record = self.root / (name + ".record")
        path = self.root / name
        path.write_text(STUB.format(record=str(record), line=line, rc=rc), encoding="utf-8")
        path.chmod(0o755)
        return str(path), record

    def start_real_daemon(self):
        """Run the queue's own daemon against the temporary root; wait until it owns its lock."""
        env = dict(self.env, GATE_QUEUE_DIR=str(self.queue), GATE_QUEUE_POLL="0.2")
        process = subprocess.Popen([sys.executable, str(QUEUE), "daemon"], cwd=self.root,
                                   env=env, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        self.addCleanup(self.stop_daemon, process)
        pidfile = self.queue / "daemon.pid"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                pid = int(pidfile.read_text().strip())
            except (OSError, ValueError):
                pid = None
            if pid is not None and Path("/proc/%d" % pid).is_dir():
                holder = self.lock_held(self.queue / "daemon.lock")
                if holder:
                    return process, pid
            if process.poll() is not None:
                self.fail("the real queue daemon exited with %r before taking its lock"
                          % process.returncode)
            time.sleep(0.05)
        self.fail("the real queue daemon never took an exclusive daemon.lock")

    def stop_daemon(self, process):
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)

    @staticmethod
    def lock_held(path):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
                return False
            except BlockingIOError:
                return True
        finally:
            os.close(fd)

    def assert_refused(self, args=None, **kwargs):
        before = snapshot(self.target)
        result = self.invoke(args, **kwargs)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("Installer refused: ", result.stderr)
        self.assertEqual(snapshot(self.target), before)
        self.assertEqual(snapshot(self.sibling), self.sibling_before)
        self.assertFalse(list(self.dest.glob("*.pending")))
        return result

    # --- the install itself ----------------------------------------------

    def test_install_writes_the_reviewed_bytes_atomically(self):
        before = snapshot(self.target)
        with self.target.open("rb") as old_descriptor:
            result = self.invoke()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(old_descriptor.read(), STALE_INSTALLED,
                             "the installer must replace, never truncate, the installed file")
            self.assertEqual(os.fstat(old_descriptor.fileno()).st_ino, before[1])
        after = snapshot(self.target)
        self.assertEqual(self.target.read_bytes(), self.reviewed)
        self.assertEqual(digest(self.target.read_bytes()), self.sha)
        self.assertNotEqual(after[1], before[1], "a real update replaces the inode")
        self.assertEqual(after[2], 0o644, "installed mode is 0644")
        self.assertEqual(snapshot(self.sibling), self.sibling_before)
        self.assertFalse(list(self.dest.glob("*.pending")))
        self.assertFalse(list(self.dest.glob(".gate_queue.py.*.pending")))
        lock = self.dest / ".gate_queue.install.lock"
        self.assertTrue(lock.is_file(), "the installer serializes itself on its own lock")
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)

        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["target"], str(self.target))
        self.assertEqual(receipt["source"], "%s:scripts/gate_queue.py" % self.commit)
        self.assertEqual(receipt["sha256"], self.sha)
        self.assertEqual(receipt["old_sha256"], digest(STALE_INSTALLED))
        self.assertEqual(receipt["mode"], 0o644)
        self.assertIs(receipt["daemon"]["live"], False)
        self.assertIs(receipt["tickets"]["blocker"], False)

        # Neither the repository nor the queue root was touched by an install.
        self.assertEqual(self.git("status", "--porcelain").stdout, "")
        self.assertEqual(self.tree_snapshot(self.queue), self.queue_before)

    def test_install_creates_a_missing_target(self):
        self.target.unlink()
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.target.read_bytes(), self.reviewed)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o644)
        self.assertIsNone(json.loads(result.stdout)["old_sha256"])

    def test_repeating_the_install_is_idempotent(self):
        self.assertEqual(self.invoke().returncode, 0)
        first = snapshot(self.target)
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.target.read_bytes(), self.reviewed)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["old_sha256"], self.sha)
        self.assertNotEqual(snapshot(self.target)[1], first[1],
                            "the installer writes atomically even when the bytes match")

    # --- the settle conditions: one live daemon, measured three ways -----

    def test_live_daemon_refusal_names_the_blocker(self):
        lock_path = str(self.queue / "daemon.lock")

        # (a) the queue's own marker: an exclusive flock on daemon.lock.
        with (self.queue / "daemon.lock").open("a+b") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            (self.queue / "daemon.pid").write_text("%d\n" % os.getpid())
            result = self.assert_refused()
            self.assertIn("the queue daemon is live:", result.stderr)
            self.assertIn(lock_path, result.stderr)
            self.assertIn("an exclusive flock", result.stderr)
            (self.queue / "daemon.pid").unlink()
        # The same root installs once nothing holds the lock.
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.target.read_bytes(), self.reviewed)

        # (b) a real `scripts/gate_queue.py daemon` process, not a marker.
        self.target.write_bytes(STALE_INSTALLED)
        process, pid = self.start_real_daemon()
        self.assertTrue(self.lock_held(self.queue / "daemon.lock"))
        result = self.assert_refused()
        self.assertIn("the queue daemon is live:", result.stderr)
        self.assertIn("/proc/%d/cmdline" % pid, result.stderr)
        self.assertIn("daemon.pid names pid %d" % pid, result.stderr)
        self.stop_daemon(process)
        self.assertFalse((self.queue / "daemon.pid").exists())
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.target.read_bytes(), self.reviewed)

        # (c) the same decision through a recording probe, run both ways.
        self.target.write_bytes(STALE_INSTALLED)
        probe, record = self.stub("daemon-probe", "daemon is live: recorded by the stub", 0)
        args = [*self.tokens(), "--daemon-probe", probe, str(self.dest)]
        result = self.assert_refused(args)
        self.assertIn("the queue daemon is live:", result.stderr)
        self.assertIn("daemon is live: recorded by the stub", result.stderr)
        lines = record.read_text().splitlines()
        self.assertEqual(len(lines), 1, "the daemon probe is run exactly once")
        kind, pid_text, recorded_args = lines[0].split(" ", 2)
        self.assertEqual(kind, "daemon")
        self.assertNotEqual(int(pid_text), os.getpid(),
                            "the probe runs as a real child process, not this one")
        self.assertEqual(recorded_args.split("|"), [str(self.queue)],
                         "the probe is handed the queue directory it must measure")

        probe, record = self.stub("daemon-probe-clear", "no daemon here", 1)
        args = [*self.tokens(), "--daemon-probe", probe, str(self.dest)]
        result = self.invoke(args)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.target.read_bytes(), self.reviewed)
        self.assertEqual(record.read_text().splitlines()[0].split(" ", 2)[0], "daemon")

    # --- the settle conditions: tickets ----------------------------------

    def test_a_ticket_that_is_not_terminal_refuses_and_names_it(self):
        self.ticket("gq-20261108T000000Z-aaaaaaaa", "done", report_dir="/r", verdict="passed")
        self.ticket("gq-20261108T000001Z-bbbbbbbb", "queued")
        self.ticket("gq-20261108T000002Z-cccccccc", "held", hold="needs-disposition")
        result = self.assert_refused()
        self.assertIn("the gate queue is not settled:", result.stderr)
        self.assertIn("gq-20261108T000001Z-bbbbbbbb", result.stderr)
        self.assertIn("'queued'", result.stderr)
        self.assertIn("gq-20261108T000002Z-cccccccc", result.stderr)
        self.assertIn("hold 'needs-disposition'", result.stderr)
        self.assertNotIn("gq-20261108T000000Z-aaaaaaaa", result.stderr)

        # Terminal states are settled: make the two non-terminal ones terminal.
        self.ticket("gq-20261108T000001Z-bbbbbbbb", "done", verdict="failed")
        self.ticket("gq-20261108T000002Z-cccccccc", "cancelled")
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["tickets"]["terminal_states"],
                         {"cancelled": 1, "done": 2})
        self.assertIs(receipt["tickets"]["blocker"], False)

    def test_a_running_ticket_and_an_unreadable_one_refuse(self):
        self.ticket("gq-20261108T000003Z-dddddddd", "running")
        result = self.assert_refused()
        self.assertIn("'running'", result.stderr)
        self.ticket("gq-20261108T000003Z-dddddddd", "done")
        broken = self.queue / "tickets" / "gq-20261108T000004Z-eeeeeeee"
        broken.mkdir()
        (broken / "ticket.json").write_text("{not json", encoding="utf-8")
        result = self.assert_refused()
        self.assertIn("gq-20261108T000004Z-eeeeeeee", result.stderr)
        self.assertIn("unreadable", result.stderr)

    def test_the_ticket_probe_seam_runs_both_ways(self):
        probe, record = self.stub("ticket-probe", "one ticket is still queued", 0)
        args = [*self.tokens(), "--ticket-probe", probe, str(self.dest)]
        result = self.assert_refused(args)
        self.assertIn("the gate queue is not settled:", result.stderr)
        self.assertIn("one ticket is still queued", result.stderr)
        kind, _, recorded_args = record.read_text().splitlines()[0].split(" ", 2)
        self.assertEqual(kind, "ticket")
        self.assertEqual(recorded_args.split("|"), [str(self.queue)])

        probe, record = self.stub("ticket-probe-clear", "all tickets are terminal", 1)
        args = [*self.tokens(), "--ticket-probe", probe, str(self.dest)]
        result = self.invoke(args)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(record.read_text().splitlines()[0].split(" ", 2)[0], "ticket")

    # --- the sha256 pin --------------------------------------------------

    def test_sha256_mismatch_refuses_naming_both_digests(self):
        wrong = "0" * 64
        args = ["--commit", self.commit, "--sha256", wrong,
                "--repo", str(self.repo), "--queue-dir", str(self.queue), str(self.dest)]
        result = self.assert_refused(args)
        self.assertIn(self.sha, result.stderr, "the reviewed digest is named")
        self.assertIn(wrong, result.stderr, "the supplied digest is named")
        self.assertIn(self.commit, result.stderr, "the commit compared against is named")

        # A pin that does not describe the named commit's bytes never installs,
        # even with --check, and never writes.
        args = ["--check", *args]
        result = self.assert_refused(args)
        self.assertIn(self.commit, result.stderr)

    def test_an_unreadable_commit_or_source_path_refuses(self):
        (self.repo / "scripts" / "gate_queue.py").unlink()
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "no queue here")
        empty = self.git("rev-parse", "HEAD").stdout.strip()
        result = self.assert_refused(["--commit", empty, "--sha256", self.sha,
                                      "--repo", str(self.repo),
                                      "--queue-dir", str(self.queue), str(self.dest)])
        self.assertIn(empty, result.stderr)
        self.assertIn("scripts/gate_queue.py", result.stderr)

        missing = "f" * 40
        result = self.assert_refused(["--commit", missing, "--sha256", self.sha,
                                      "--repo", str(self.repo),
                                      "--queue-dir", str(self.queue), str(self.dest)])
        self.assertIn(missing, result.stderr)

        result = self.assert_refused([*self.tokens(), "--repo", str(self.root / "absent"),
                                      str(self.dest)])
        self.assertIn("absent", result.stderr)

    def test_malformed_arguments_refuse(self):
        base = ["--repo", str(self.repo), "--queue-dir", str(self.queue)]
        cases = {
            "no sha256": ["--commit", self.commit, *base, str(self.dest)],
            "no commit": ["--sha256", self.sha, *base, str(self.dest)],
            "short commit": ["--commit", self.commit[:12], "--sha256", self.sha, *base,
                             str(self.dest)],
            "uppercase commit": ["--commit", self.commit.upper(), "--sha256", self.sha, *base,
                                 str(self.dest)],
            "short sha256": ["--commit", self.commit, "--sha256", self.sha[:32], *base,
                             str(self.dest)],
            "two targets": [*self.tokens(), str(self.dest), str(self.dest)],
        }
        for name, args in cases.items():
            with self.subTest(name):
                self.assert_refused(args)

    # --- --check never writes --------------------------------------------

    def test_check_reports_drift_and_match_without_writing(self):
        before = snapshot(self.target)
        result = self.invoke(["--check", *self.tokens(), str(self.dest)])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(digest(STALE_INSTALLED), result.stdout, "the installed digest is printed")
        self.assertIn(self.sha, result.stdout, "the reviewed digest is printed")
        self.assertIn(self.commit, result.stdout, "the commit compared against is printed")
        self.assertIn("drift", result.stdout)
        self.assertEqual(snapshot(self.target), before, "--check must not write")
        self.assertFalse((self.dest / ".gate_queue.install.lock").exists(),
                         "--check must not even create the installer lock")
        self.assertFalse(list(self.dest.glob("*.pending")))

        self.target.write_bytes(self.reviewed)
        installed = snapshot(self.target)
        result = self.invoke(["--check", *self.tokens(), str(self.dest)])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("match", result.stdout)
        self.assertIn(self.sha, result.stdout)
        self.assertEqual(snapshot(self.target), installed)

        self.target.unlink()
        absent = self.invoke(["--check", *self.tokens(), str(self.dest)])
        self.assertEqual(absent.returncode, 1, absent.stdout + absent.stderr)
        self.assertIn("absent", absent.stdout)
        self.assertIn("none", absent.stdout)
        self.assertFalse(self.target.exists(), "--check never creates the target")
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()), ["unrelated"])

    def test_check_refuses_an_unreadable_commit_without_writing(self):
        result = self.assert_refused(["--check", "--commit", "b" * 40, "--sha256", self.sha,
                                      "--repo", str(self.repo),
                                      "--queue-dir", str(self.queue), str(self.dest)])
        self.assertIn("b" * 40, result.stderr)

    # --- filesystem refusals ---------------------------------------------

    def test_symlink_target_refuses_without_touching_the_referent(self):
        referent = self.dest / "old-referent"
        self.target.rename(referent)
        self.target.symlink_to(referent)
        before = snapshot(referent)
        result = self.assert_refused()
        self.assertIn("not a regular file", result.stderr)
        self.assertTrue(self.target.is_symlink())
        self.assertEqual(snapshot(referent), before)

    def test_nonregular_target_refuses_without_blocking(self):
        self.target.unlink()
        os.mkfifo(self.target)
        before = snapshot(self.target)
        result = self.assert_refused()
        self.assertEqual(snapshot(self.target)[1:], before[1:])
        self.assertTrue(stat.S_ISFIFO(os.lstat(self.target).st_mode))

    def test_held_installer_lock_refuses(self):
        lock = self.dest / ".gate_queue.install.lock"
        with lock.open("a+b") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = snapshot(lock)
            result = self.assert_refused()
            self.assertIn("another installer holds", result.stderr)
            self.assertIn(str(lock), result.stderr)
            self.assertEqual(snapshot(lock), before)

    def test_missing_target_directory_refuses(self):
        missing = self.root / "not-created"
        result = self.assert_refused([*self.tokens(), str(missing)])
        self.assertIn("does not exist", result.stderr)
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
