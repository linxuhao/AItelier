# tests/unit/test_build_runner.py
# Unit tests for core/build_runner.py

import sys
import json
import pytest
from pathlib import Path
from core.build_runner import BuildRunner, _read_package_json


class TestLanguageDetection:
    def test_detects_python_from_py_files(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "main.py").write_text("print('hello')", encoding="utf-8")

        runner = BuildRunner()
        langs = runner._detect_languages(project_dir)
        assert "python" in langs
        assert "node" not in langs

    def test_detects_python_from_pyproject(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text("[project]\nname = 'test'", encoding="utf-8")

        runner = BuildRunner()
        langs = runner._detect_languages(project_dir)
        assert "python" in langs

    def test_detects_node_from_package_json(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "package.json").write_text('{"name": "test"}', encoding="utf-8")

        runner = BuildRunner()
        langs = runner._detect_languages(project_dir)
        assert "node" in langs

    def test_detects_nothing_in_empty_dir(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        runner = BuildRunner()
        langs = runner._detect_languages(project_dir)
        assert len(langs) == 0

    def test_detects_both_python_and_node(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "main.py").write_text("pass", encoding="utf-8")
        (project_dir / "package.json").write_text('{"name": "test"}', encoding="utf-8")

        runner = BuildRunner()
        langs = runner._detect_languages(project_dir)
        assert "python" in langs
        assert "node" in langs


class TestPythonChecks:
    def test_compile_valid_python(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "main.py").write_text("def hello():\n    return 'world'\n", encoding="utf-8")

        runner = BuildRunner()
        checks = runner._check_python(project_dir)

        compile_check = next(c for c in checks if c["name"] == "python_compile")
        assert compile_check["passed"] is True

    def test_compile_invalid_python(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "broken.py").write_text("def broken(\n  pass\n", encoding="utf-8")

        runner = BuildRunner()
        checks = runner._check_python(project_dir)

        compile_check = next(c for c in checks if c["name"] == "python_compile")
        assert compile_check["passed"] is False
        assert "broken.py" in compile_check["output"]

    def test_tests_pass(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "test_main.py").write_text(
            "def test_basic():\n    assert 1 + 1 == 2\n", encoding="utf-8"
        )

        runner = BuildRunner()
        checks = runner._check_python(project_dir)

        test_check = next(c for c in checks if c["name"] == "python_tests")
        assert test_check["passed"] is True

    def test_tests_fail(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "test_fail.py").write_text(
            "def test_broken():\n    assert False, 'intentional failure'\n", encoding="utf-8"
        )

        runner = BuildRunner()
        checks = runner._check_python(project_dir)

        test_check = next(c for c in checks if c["name"] == "python_tests")
        assert test_check["passed"] is False
        assert "intentional failure" in test_check["output"]

    def test_no_tests_skips_test_run(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "main.py").write_text("x = 1\n", encoding="utf-8")

        runner = BuildRunner()
        checks = runner._check_python(project_dir)

        test_check = next(c for c in checks if c["name"] == "python_tests")
        assert test_check["passed"] is True
        assert "No test files" in test_check["output"]


class TestRunChecks:
    def test_no_project_dir_passes(self, tmp_path):
        runner = BuildRunner()
        result = runner.run_checks(tmp_path, "4")
        assert result["passed"] is True

    def test_empty_project_passes(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        runner = BuildRunner()
        # run_checks takes code_path directly now
        result = runner.run_checks(project_dir, "4")
        assert result["passed"] is True
        assert "skipping" in result["summary"]

    def test_valid_python_project_passes(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "main.py").write_text("x = 1\n", encoding="utf-8")
        (project_dir / "test_main.py").write_text("def test_ok(): assert True\n", encoding="utf-8")

        runner = BuildRunner()
        result = runner.run_checks(project_dir, "4")
        assert result["passed"] is True
        assert "python_compile: OK" in result["summary"]

    def test_broken_syntax_fails(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "bad.py").write_text("def bad(\n", encoding="utf-8")

        runner = BuildRunner()
        result = runner.run_checks(project_dir, "4")
        assert result["passed"] is False
        assert "python_compile: FAILED" in result["summary"]


class TestBuildSummary:
    def test_all_pass(self):
        runner = BuildRunner()
        summary = runner._build_summary([
            {"name": "build", "passed": True, "output": "ok"},
            {"name": "tests", "passed": True, "output": "3 passed"},
        ])
        assert summary == "build: OK | tests: OK"

    def test_mixed_results(self):
        runner = BuildRunner()
        summary = runner._build_summary([
            {"name": "build", "passed": True, "output": "ok"},
            {"name": "tests", "passed": False, "output": "1 failed"},
        ])
        assert summary == "build: OK | tests: FAILED"

    def test_empty_checks(self):
        runner = BuildRunner()
        summary = runner._build_summary([])
        assert summary == "No checks run"


class TestReadPackageJson:
    def test_reads_valid(self, tmp_path):
        (tmp_path / "package.json").write_text(
            '{"name": "test", "scripts": {"build": "tsc"}}', encoding="utf-8"
        )
        result = _read_package_json(tmp_path)
        assert result["name"] == "test"

    def test_missing_file(self, tmp_path):
        result = _read_package_json(tmp_path)
        assert result is None

    def test_invalid_json(self, tmp_path):
        (tmp_path / "package.json").write_text("not json", encoding="utf-8")
        result = _read_package_json(tmp_path)
        assert result is None
