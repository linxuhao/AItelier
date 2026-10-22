"""Real installer subprocess tests; can run copied standalone with unittest.

Set GATE_INSTALLER_SOURCE_ROOT to the exact checkout under review. No app/SDK
imports are needed. Run only in the prescribed disposable offline CPU capsule.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(os.environ.get("GATE_INSTALLER_SOURCE_ROOT", Path(__file__).resolve().parents[2]))
BASE = "f5472a378a4b965a76db138fd2713c96807722c9"


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def snapshot(path):
    s = path.lstat()
    return (path.read_bytes(), s.st_ino, stat.S_IMODE(s.st_mode), s.st_uid,
            s.st_gid, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


class GateRunInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        for name in ("install_gate_run.sh", "install_gate_run.py", "gate_run.sh", "gate_binding.py"):
            source = ROOT / "tools" / name
            if source.exists():
                shutil.copyfile(source, self.source / name)
        self.dest = self.root / "bin"
        self.dest.mkdir()
        self.target = self.dest / "gate_run.sh"
        self.target.write_bytes(b"#!/bin/sh\nprintf 'old launcher\\n'\n")
        self.target.chmod(0o751)
        self.helper = self.dest / "gate_binding.py"
        self.helper.write_bytes(b"# installed helper must stay byte-identical\n")
        self.helper.chmod(0o640)
        self.sibling = self.dest / "unrelated"
        self.sibling.write_bytes(b"unrelated sibling\n")
        self.old = digest(self.target.read_bytes())
        self.new = digest((self.source / "gate_run.sh").read_bytes())
        self.helper_before = snapshot(self.helper)
        self.sibling_before = snapshot(self.sibling)

    def invoke(self, args=None, **kwargs):
        if args is None:
            args = ["--launcher-only", self.old, self.new, str(self.dest)]
        return subprocess.run(["sh", str(self.source / "install_gate_run.sh"), *args],
                              cwd=self.root, capture_output=True, text=True, timeout=10, **kwargs)

    def assert_preserved(self):
        self.assertEqual(snapshot(self.helper), self.helper_before)
        self.assertEqual(snapshot(self.sibling), self.sibling_before)
        self.assertFalse(list(self.dest.glob("*.pending")))

    def assert_refused(self, args=None):
        before = snapshot(self.target) if self.target.is_file() else None
        result = self.invoke(args)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        if before is not None:
            self.assertEqual(snapshot(self.target), before)
        self.assert_preserved()
        return result

    def test_useful_atomic_update_keeps_old_descriptor_and_real_cpu_child(self):
        child = subprocess.Popen([sys.executable, "-u", "-c",
            "import time\nprint('ready', flush=True)\nend=time.monotonic()+30\n"
            "n=0\nwhile time.monotonic()<end: n=(n+1)%1000003\n"], stdout=subprocess.PIPE)
        try:
            self.assertTrue(select.select([child.stdout], [], [], 5)[0], "CPU child did not start")
            self.assertEqual(child.stdout.readline(), b"ready\n")
            before = snapshot(self.target)
            with self.target.open("rb") as old_descriptor:
                self.assertIsNone(child.poll())
                result = self.invoke()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIsNone(child.poll(), "Unrelated real CPU process was stopped")
                self.assertEqual(old_descriptor.read(), before[0])
                self.assertEqual(os.fstat(old_descriptor.fileno()).st_ino, before[1])
            self.assertEqual(self.target.read_bytes(), (self.source / "gate_run.sh").read_bytes())
            after = snapshot(self.target)
            self.assertNotEqual(after[1], before[1], "Update must replace inode, not truncate it")
            self.assertEqual(after[2:5], before[2:5])
            self.assertEqual(json.loads(result.stdout)["old_sha256"], self.old)
            self.assert_preserved()
        finally:
            if child.poll() is None:
                child.terminate()
            child.communicate(timeout=5)

    def test_stale_installed_hash_refuses(self):
        self.assert_refused(["--launcher-only", "0" * 64, self.new, str(self.dest)])

    def test_wrong_source_hash_refuses(self):
        self.assert_refused(["--launcher-only", self.old, "0" * 64, str(self.dest)])

    def test_malformed_and_missing_arguments_refuse(self):
        cwd_target = self.root / "gate_run.sh"
        cwd_target.write_bytes(self.target.read_bytes())
        cwd_before = snapshot(cwd_target)
        for args in (["--launcher-only"], ["--launcher-only", self.old],
                     ["--launcher-only", self.old, "bad", str(self.dest)],
                     ["--launcher-only", self.old.upper(), self.new, str(self.dest)],
                     ["--launcher-only", self.old, self.new, str(self.dest), "extra"],
                     ["--launcher-only", self.old, self.new, ""],
                     ["--unknown", str(self.dest)]):
            with self.subTest(args=args):
                self.assert_refused(args)
                self.assertEqual(snapshot(cwd_target), cwd_before)

    def test_missing_installed_file_refuses_without_creating_it(self):
        self.target.unlink()
        self.assert_refused()
        self.assertFalse(self.target.exists())

    def test_missing_source_file_refuses(self):
        (self.source / "gate_run.sh").unlink()
        self.assert_refused()

    def test_missing_destination_refuses_without_creating_it(self):
        missing = self.root / "not-created"
        self.assert_refused(["--launcher-only", self.old, self.new, str(missing)])
        self.assertFalse(missing.exists())

    def test_symlink_source_refuses(self):
        source = self.source / "gate_run.sh"
        raw = source.read_bytes()
        source.unlink()
        other = self.source / "other-launcher"
        other.write_bytes(raw)
        source.symlink_to(other)
        self.assert_refused()

    def test_symlink_target_refuses_without_touching_referent(self):
        other = self.dest / "old-referent"
        self.target.rename(other)
        self.target.symlink_to(other)
        before = snapshot(other)
        self.assert_refused()
        self.assertTrue(self.target.is_symlink())
        self.assertEqual(snapshot(other), before)

    def test_nonregular_source_refuses_without_blocking(self):
        source = self.source / "gate_run.sh"
        source.unlink()
        os.mkfifo(source)
        self.assert_refused()

    def test_unknown_lock_symlink_refuses(self):
        other = self.root / "unknown-lock-referent"
        other.write_bytes(b"do not modify\n")
        before = snapshot(other)
        (self.dest / ".gate_run.install.lock").symlink_to(other)
        self.assert_refused()
        self.assertEqual(snapshot(other), before)

    def test_ordinary_mode_still_installs_both_files(self):
        result = self.invoke([str(self.dest)])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name in ("gate_run.sh", "gate_binding.py"):
            self.assertEqual((self.dest / name).read_bytes(), (self.source / name).read_bytes())
            self.assertEqual(stat.S_IMODE((self.dest / name).stat().st_mode), 0o755)
        self.assertEqual(snapshot(self.sibling), self.sibling_before)
        result = self.invoke([""], env=dict(os.environ, HOME=str(self.root)))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        default = self.root / ".AItelier/bin"
        for name in ("gate_run.sh", "gate_binding.py"):
            self.assertEqual((default / name).read_bytes(), (self.source / name).read_bytes())

    def test_two_actual_installers_exclude_during_ordinary_install(self):
        # A FIFO pauses the real install subprocess without changing its eventual
        # filesystem operation. Both contenders invoke the actual normal entry.
        wrapper_dir = self.root / "instrumented-path"
        wrapper_dir.mkdir()
        ready, release = self.root / "ready", self.root / "release"
        os.mkfifo(ready)
        os.mkfifo(release)
        ready_fd = os.open(ready, os.O_RDWR | os.O_NONBLOCK)
        release_fd = os.open(release, os.O_RDWR | os.O_NONBLOCK)
        env = dict(os.environ, PATH=str(wrapper_dir) + os.pathsep + os.environ["PATH"],
                   READY=str(ready), RELEASE=str(release), ONCE=str(self.root / "once"))
        real_install = shutil.which("install")
        self.assertIsNotNone(real_install)
        wrapper = wrapper_dir / "install"
        wrapper.write_text('#!/bin/sh\nset -eu\nif mkdir "$ONCE" 2>/dev/null; then\n'
                           ' printf ready > "$READY"\n read -r release < "$RELEASE"\nfi\n'
                           'exec ' + shlex.quote(real_install) + ' "$@"\n')
        wrapper.chmod(0o755)
        first = subprocess.Popen(["sh", str(self.source / "install_gate_run.sh"), str(self.dest)],
                                 cwd=self.root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertTrue(select.select([ready_fd], [], [], 5)[0], "First real installer did not reach barrier")
            self.assertEqual(os.read(ready_fd, 5), b"ready")
            self.assertIsNone(first.poll())
            self.assert_refused()
            self.assert_refused([str(self.dest)])
            os.write(release_fd, b"continue\n")
            stdout, stderr = first.communicate(timeout=10)
            self.assertEqual(first.returncode, 0, (stdout, stderr))
            self.assertEqual(self.target.read_bytes(), (self.source / "gate_run.sh").read_bytes())
            self.assertEqual(self.helper.read_bytes(), (self.source / "gate_binding.py").read_bytes())
        finally:
            os.write(release_fd, b"continue\n")
            if first.poll() is None:
                first.kill()
            first.communicate(timeout=5)
            os.close(ready_fd)
            os.close(release_fd)

    def test_actual_prior_installer_counterexample_overwrites_helper(self):
        # Retain the actual frozen parent script, rather than a rewritten model
        # of its behavior. This is a measured limitation of ordinary old install.
        result = subprocess.run(["git", "--no-optional-locks", "-C", str(ROOT), "show",
                                 BASE + ":tools/install_gate_run.sh"], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        prior = self.source / "prior-install.sh"
        prior.write_bytes(result.stdout)
        before_helper = self.helper.read_bytes()
        result = subprocess.run(["sh", str(prior), str(self.dest)], cwd=self.root,
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(self.helper.read_bytes(), before_helper)
        self.assertEqual(self.helper.read_bytes(), (self.source / "gate_binding.py").read_bytes())
        self.assertEqual(snapshot(self.sibling), self.sibling_before)


class GateBindingInstallerTests(unittest.TestCase):
    def setUp(self):
        GateRunInstallerTests.setUp(self)
        self.launcher = self.target
        self.launcher_before = snapshot(self.launcher)
        self.target = self.helper
        self.old = digest(self.target.read_bytes())
        self.new = digest((self.source / "gate_binding.py").read_bytes())

    def invoke(self, args=None, **kwargs):
        if args is None:
            args = ["--helper-only", self.old, self.new, str(self.dest)]
        return subprocess.run(["sh", str(self.source / "install_gate_run.sh"), *args],
                              cwd=self.root, capture_output=True, text=True, timeout=10, **kwargs)

    def assert_preserved(self):
        self.assertEqual(snapshot(self.launcher), self.launcher_before)
        self.assertEqual(snapshot(self.sibling), self.sibling_before)
        self.assertFalse(list(self.dest.glob("*.pending")))

    assert_refused = GateRunInstallerTests.assert_refused
    test_missing_installed_file_refuses_without_creating_it = GateRunInstallerTests.test_missing_installed_file_refuses_without_creating_it
    test_unknown_lock_symlink_refuses = GateRunInstallerTests.test_unknown_lock_symlink_refuses

    def test_useful_atomic_helper_update_preserves_metadata_and_old_descriptor(self):
        before = snapshot(self.target)
        with self.target.open("rb") as old_descriptor:
            result = self.invoke()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(old_descriptor.read(), before[0])
            self.assertEqual(os.fstat(old_descriptor.fileno()).st_ino, before[1])
        after = snapshot(self.target)
        self.assertEqual(after[0], (self.source / "gate_binding.py").read_bytes())
        self.assertNotEqual(after[1], before[1])
        self.assertEqual(after[2:5], before[2:5])
        self.assertEqual(digest(after[0]), self.new)
        self.assertEqual(json.loads(result.stdout), dict(old_sha256=self.old, sha256=self.new,
                         mode=before[2], uid=before[3], gid=before[4]))
        self.assert_preserved()
        lock = self.dest / ".gate_run.install.lock"
        lock_inode = lock.stat().st_ino
        self.assert_refused()
        self.assertEqual(lock.stat().st_ino, lock_inode)

    def test_incorrect_old_and_source_hashes_refuse(self):
        for old, new in (("0" * 64, self.new), (self.old, "0" * 64)):
            with self.subTest(old=old, new=new):
                self.assert_refused(["--helper-only", old, new, str(self.dest)])

    def test_changed_source_pin_refuses(self):
        (self.source / "gate_binding.py").write_bytes(b"# different source after pinning\n")
        self.assert_refused()

    def test_malformed_arguments_refuse(self):
        for args in (["--helper-only"], ["--helper-only", self.old],
                     ["--helper-only", self.old, "bad", str(self.dest)],
                     ["--helper-only", self.old.upper(), self.new, str(self.dest)],
                     ["--helper-only", self.old, self.new, str(self.dest), "extra"],
                     ["--helper-only", self.old, self.new, ""]):
            with self.subTest(args=args):
                result = self.assert_refused(args)
                self.assertIn("Usage: install_gate_run.sh --helper-only", result.stderr)

    def test_missing_source_refuses(self):
        (self.source / "gate_binding.py").unlink()
        self.assert_refused()

    def test_missing_destination_refuses_without_creating_it(self):
        missing = self.root / "not-created"
        self.assert_refused(["--helper-only", self.old, self.new, str(missing)])
        self.assertFalse(missing.exists())

    def check_symlink_refusal(self, path):
        other = path.with_name("helper-referent")
        path.rename(other)
        path.symlink_to(other)
        before = snapshot(other)
        self.assert_refused()
        self.assertTrue(path.is_symlink())
        self.assertEqual(snapshot(other), before)

    def test_source_symlink_refuses(self):
        self.check_symlink_refusal(self.source / "gate_binding.py")

    def test_target_symlink_refuses(self):
        self.check_symlink_refusal(self.target)

    def check_nonregular_refusal(self, path):
        path.unlink()
        os.mkfifo(path)
        before = path.lstat()
        self.assert_refused()
        after = path.lstat()
        self.assertTrue(stat.S_ISFIFO(after.st_mode))
        self.assertEqual((after.st_ino, after.st_mode, after.st_uid, after.st_gid),
                         (before.st_ino, before.st_mode, before.st_uid, before.st_gid))

    def test_source_nonregular_refuses_without_blocking(self):
        self.check_nonregular_refusal(self.source / "gate_binding.py")

    def test_target_nonregular_refuses_without_blocking(self):
        self.check_nonregular_refusal(self.target)

    def test_held_persistent_lock_refuses(self):
        lock = self.dest / ".gate_run.install.lock"
        with lock.open("a+b") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = snapshot(lock)
            self.assert_refused()
            self.assertEqual(snapshot(lock), before)

    def test_real_late_target_change_refuses_before_replacement(self):
        # Pause only after the real pending-file fsync. The actual entry point,
        # reads, hash checks, write, metadata changes and replacement stay intact.
        instrument = self.root / "instrument"
        instrument.mkdir()
        ready, release = self.root / "ready", self.root / "release"
        os.mkfifo(ready)
        os.mkfifo(release)
        ready_fd = os.open(ready, os.O_RDWR | os.O_NONBLOCK)
        release_fd = os.open(release, os.O_RDWR | os.O_NONBLOCK)
        (instrument / "sitecustomize.py").write_text(
            'import os, stat\n'
            '_fsync = os.fsync\n'
            'def barrier(fd):\n'
            '    _fsync(fd)\n'
            '    if stat.S_ISREG(os.fstat(fd).st_mode):\n'
            '        with open(os.environ["READY"], "wb") as ready:\n'
            '            ready.write(b"ready")\n'
            '        with open(os.environ["RELEASE"], "rb") as release:\n'
            '            release.readline()\n'
            'os.fsync = barrier\n')
        env = dict(os.environ, PYTHONPATH=str(instrument), READY=str(ready), RELEASE=str(release))
        process = subprocess.Popen(["sh", str(self.source / "install_gate_run.sh"),
                                    "--helper-only", self.old, self.new, str(self.dest)],
                                   cwd=self.root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertTrue(select.select([ready_fd], [], [], 5)[0], "Installer did not reach fsync barrier")
            self.assertEqual(os.read(ready_fd, 5), b"ready")
            self.assertIsNone(process.poll())
            self.assertEqual(digest(self.target.read_bytes()), self.old)
            pending = list(self.dest.glob("*.pending"))
            self.assertEqual(len(pending), 1)
            self.assertEqual(digest(pending[0].read_bytes()), self.new)
            self.target.write_bytes(b"# real competing helper change\n")
            changed = snapshot(self.target)
            os.write(release_fd, b"continue\n")
            stdout, stderr = process.communicate(timeout=10)
            self.assertNotEqual(process.returncode, 0, (stdout, stderr))
            self.assertIn(b"Installed helper changed before replacement", stderr)
            self.assertEqual(snapshot(self.target), changed)
            self.assert_preserved()
        finally:
            os.write(release_fd, b"continue\n")
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
            os.close(ready_fd)
            os.close(release_fd)


if __name__ == "__main__":
    unittest.main(verbosity=2)
