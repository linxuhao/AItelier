"""Real installer subprocess tests; can run copied standalone with unittest.

Set GATE_INSTALLER_SOURCE_ROOT to the exact checkout under review. No app/SDK
imports are needed. Run only in the prescribed disposable offline CPU capsule.
"""
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
