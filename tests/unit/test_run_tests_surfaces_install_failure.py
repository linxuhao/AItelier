"""A failed project install must be reported, not swallowed.

`run_tests` provisions a venv and does `pip install -e .`. That call was made with
`check=False` AND wrapped in `except Exception: pass`, so its failure was discarded
twice over — and it is the one fact that explains everything downstream.

Observed live: a generated project declared

    build-backend = "setuptools.backends._legacy:_Backend"     # does not exist

so the install died with `BackendUnavailable: Cannot import
'setuptools.backends._legacy'` — an error naming the exact cause. Thrown away. pytest
then reported `ModuleNotFoundError: No module named 'word_frequency'`, and the maker,
reading a symptom with the cause removed, concluded "package discovery" and rewrote
`[tool.setuptools.packages.find]` — which was never wrong. Same loop every lap, until
the fix budget ran out.

The install still must NOT fail the gate: a generated project can legitimately be an
app rather than an installable package.
"""
import os
import subprocess

import pytest

from aitelier.tools.run_tests.impl import _install_project_deps


class _Proc:
    def __init__(self, rc, stderr="", stdout=""):
        self.returncode, self.stderr, self.stdout = rc, stderr, stdout


class TestItReturnsTheReason:
    def test_a_failed_install_returns_its_stderr(self, tmp_path, monkeypatch):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc(
            1, "BackendUnavailable: Cannot import 'setuptools.backends._legacy'"))
        err = _install_project_deps("py", tmp_path)
        assert "BackendUnavailable" in err

    def test_a_successful_install_returns_empty(self, tmp_path, monkeypatch):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc(0))
        assert _install_project_deps("py", tmp_path) == ""

    def test_nothing_to_install_is_not_an_error(self, tmp_path):
        assert _install_project_deps("py", tmp_path) == ""

    def test_it_still_never_raises(self, tmp_path, monkeypatch):
        """A non-installable generated project must not fail the gate."""
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")

        def boom(*a, **k):
            raise OSError("no pip")
        monkeypatch.setattr(subprocess, "run", boom)
        assert "OSError" in _install_project_deps("py", tmp_path)

    def test_requirements_txt_takes_precedence(self, tmp_path, monkeypatch):
        (tmp_path / "requirements.txt").write_text("pytest\n")
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        seen = {}
        monkeypatch.setattr(subprocess, "run",
                            lambda cmd, **k: (seen.update(cmd=cmd), _Proc(0))[1])
        _install_project_deps("py", tmp_path)
        assert "-r" in seen["cmd"]

    def test_stderr_is_truncated_so_it_stays_a_pointer(self, tmp_path, monkeypatch):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **k: _Proc(1, "x" * 5000))
        assert len(_install_project_deps("py", tmp_path)) <= 600


class TestTheReasonIsTheUsefulLine:
    """A raw tail is traceback noise. The exception line is the whole point."""

    PIP_FAILURE = (
        'Traceback (most recent call last):\n'
        '  File "/usr/lib/pip/_vendor/pyproject_hooks/_impl.py", line 402, in _call_hook\n'
        '    raise BackendUnavailable(\n'
        '           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n'
        "pip._vendor.pyproject_hooks._impl.BackendUnavailable: "
        "Cannot import 'setuptools.backends._legacy'\n")

    def test_it_picks_the_exception_not_the_caret_ruler(self, tmp_path, monkeypatch):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **k: _Proc(1, self.PIP_FAILURE))
        err = _install_project_deps("py", tmp_path)
        assert "Cannot import 'setuptools.backends._legacy'" in err
        assert "^^^" not in err
        assert not err.startswith("File ")

    def test_it_falls_back_when_nothing_looks_like_an_exception(self, tmp_path,
                                                               monkeypatch):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **k: _Proc(1, "a\nb\nc\n"))
        assert _install_project_deps("py", tmp_path)

    def test_an_empty_failure_still_says_something(self, tmp_path, monkeypatch):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc(2, "", ""))
        assert "exit 2" in _install_project_deps("py", tmp_path)


class TestTheReportLeadsWithTheCause:
    """Ordering is the point: the cause must come ABOVE the symptom."""

    from aitelier.tools.run_tests.impl import _lead_with_install_error as _lead
    lead = staticmethod(_lead)

    PYTEST_OUT = "E   ModuleNotFoundError: No module named 'word_frequency'"

    def test_the_install_error_comes_before_the_pytest_output(self):
        """Compare against the pytest OUTPUT, not the word ModuleNotFoundError —
        the explanatory prose names it deliberately, above the error itself."""
        err = "BackendUnavailable: Cannot import 'x'"
        out = self.lead({"passed": False, "summary": self.PYTEST_OUT,
                         "install_error": err})
        assert out.index(err) < out.index(self.PYTEST_OUT)
        assert "Fix this FIRST" in out

    def test_a_passing_suite_is_not_annotated(self):
        out = self.lead({"passed": True, "summary": "all good",
                         "install_error": "BackendUnavailable: Cannot import 'x'"})
        assert out == "all good"

    def test_no_install_error_leaves_the_summary_alone(self):
        out = self.lead({"passed": False, "summary": self.PYTEST_OUT})
        assert out == self.PYTEST_OUT

    def test_it_says_the_symptom_is_probably_a_consequence(self):
        """Without this the reader 'fixes' packaging metadata that was never wrong."""
        out = self.lead({"passed": False, "summary": self.PYTEST_OUT,
                         "install_error": "boom"})
        assert "consequence" in out and "packaging-discovery" in out


class TestSrcLayoutIsImportable:
    """A `src/` layout must be able to import its own package under pytest.

    Only the repo ROOT was on PYTHONPATH, so a flat layout worked and the standard
    `src/` layout could not import itself at all — every test module died on
    `ModuleNotFoundError`, every attempt, unfixably. The editable install that
    would otherwise cover it runs ONLY on the venv-provisioning path, and
    `_resolve_pytest_python` short-circuits to the current interpreter whenever
    pytest is already importable — which in the container it always is. So on the
    common path the project under test was never installed and nothing put `src`
    on the path.
    """
    from aitelier.tools.run_tests.impl import _pythonpath_for as _pp
    pp = staticmethod(_pp)

    def test_src_is_added_when_present(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PYTHONPATH", raising=False)
        (tmp_path / "src").mkdir()
        parts = self.pp(tmp_path).split(os.pathsep)
        assert parts == [str(tmp_path), str(tmp_path / "src")]

    def test_a_flat_layout_is_unchanged(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PYTHONPATH", raising=False)
        assert self.pp(tmp_path) == str(tmp_path)

    def test_a_src_FILE_is_not_mistaken_for_a_package_root(self, tmp_path,
                                                           monkeypatch):
        monkeypatch.delenv("PYTHONPATH", raising=False)
        (tmp_path / "src").write_text("not a directory")
        assert self.pp(tmp_path) == str(tmp_path)

    def test_an_inherited_pythonpath_is_preserved_last(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PYTHONPATH", "/opt/thing")
        (tmp_path / "src").mkdir()
        assert self.pp(tmp_path).endswith(os.pathsep + "/opt/thing")

    def test_the_repo_root_still_comes_first(self, tmp_path, monkeypatch):
        """Repo-root imports (conftest, top-level packages) must keep winning."""
        monkeypatch.delenv("PYTHONPATH", raising=False)
        (tmp_path / "src").mkdir()
        assert self.pp(tmp_path).split(os.pathsep)[0] == str(tmp_path)


class TestImportErrorsNameWhatIsActuallyThere:
    """`cannot import name X from Y` means Y imported FINE — the answer is right there.

    An agent writing against a third-party API cannot check it: the read tools are
    closures over project root / step staging / step output, so `site-packages` is
    unreachable by design. It guessed `InitializationCapabilities` from
    `mcp.server.models` on three separate drives; the real name is
    `InitializationOptions`, and the module was importable in the very interpreter
    that raised. Same shape as `file_exists` listing only files while the directory
    it claimed was missing sat right there: show what IS there.
    """
    import sys as _sys
    from aitelier.tools.run_tests.impl import _explain_missing_names as _ex
    ex = staticmethod(_ex)
    PY = _sys.executable

    def test_it_lists_the_real_exports(self):
        out = self.ex(self.PY,
                      "E   ImportError: cannot import name 'Nope' from 'json'")
        assert "JSONDecoder" in out and "dumps" in out
        assert "'json' has no 'Nope'" in out

    def test_it_suggests_the_near_miss(self):
        out = self.ex(self.PY,
                      "ImportError: cannot import name 'JSONDecodeErr' from 'json'")
        assert "Closest by name" in out and "JSONDecodeError" in out

    def test_the_original_summary_is_preserved(self):
        src = "E   ImportError: cannot import name 'Nope' from 'json'"
        assert self.ex(self.PY, src).startswith(src)

    def test_an_unrelated_failure_is_untouched(self):
        src = "E   assert 1 == 2"
        assert self.ex(self.PY, src) == src

    def test_an_unimportable_module_is_skipped_quietly(self):
        src = "ImportError: cannot import name 'X' from 'no_such_module_xyz'"
        assert self.ex(self.PY, src) == src

    def test_each_module_is_reported_once(self):
        src = ("ImportError: cannot import name 'A' from 'json'\n"
               "ImportError: cannot import name 'A' from 'json'")
        assert self.ex(self.PY, src).count("'json' has no") == 1


class TestAttributeErrorsNameWhatTheObjectHas:
    """One layer deeper than the import: `'X' object has no attribute 'y'`.

    The class is real and importable — it is named in a `from M import X` line in
    the same traceback — so its attributes are knowable. Observed immediately after
    the import-level guess was fixed: the agent moved to `@server.tool()`, which is
    FastMCP's decorator and does not exist on the low-level `Server`. Without this
    the failure walks one attribute at a time through the whole fix budget.
    """
    import sys as _sys
    from aitelier.tools.run_tests.impl import _explain_missing_names as _ex
    ex = staticmethod(_ex)
    PY = _sys.executable

    TRACE = ("tests/t.py:4: in <module>\n"
             "    from json import JSONDecoder\n"
             "E   AttributeError: 'JSONDecoder' object has no attribute 'decode_all'\n")

    def test_it_lists_the_real_attributes(self):
        out = self.ex(self.PY, self.TRACE)
        assert "'JSONDecoder' (from json) has no 'decode_all'" in out
        assert "raw_decode" in out

    def test_it_suggests_the_near_miss(self):
        out = self.ex(self.PY, self.TRACE)
        assert "Closest by name" in out and "decode" in out

    def test_it_runs_even_when_there_is_no_import_error(self):
        """Regression: an early return for 'no cannot-import-name hits' meant the
        attribute pass never ran at all."""
        assert "attributes are" in self.ex(self.PY, self.TRACE)

    def test_both_passes_can_fire_together(self):
        both = ("from json import JSONDecoder\n"
                "ImportError: cannot import name 'Nope' from 'json'\n"
                "AttributeError: 'JSONDecoder' object has no attribute 'decode_all'\n")
        out = self.ex(self.PY, both)
        assert "'json' has no 'Nope'" in out
        assert "'JSONDecoder' (from json) has no 'decode_all'" in out

    def test_no_import_line_means_no_guessing(self):
        src = "AttributeError: 'Whatever' object has no attribute 'x'"
        assert self.ex(self.PY, src) == src

    def test_an_unrelated_failure_is_untouched(self):
        assert self.ex(self.PY, "E   assert 1 == 2") == "E   assert 1 == 2"
