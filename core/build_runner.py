# core/build_runner.py
# [说明] 后置验证层。在 Green-Gate-Red 全部通过且文件回写 project/ 之后，
#        执行确定性构建/测试命令，验证代码在运行时真正可用。
#        失败时将结果反馈给 Green Agent 重试。

import os
import sys
import shutil
from pathlib import Path
from core.tool_executor import SecureToolRunner


class BuildRunner:
    """
    确定性构建与测试执行器。
    在 project/ 工作区上运行 build + test 命令，返回结构化结果。
    """

    def __init__(self, timeout: int = 120):
        self.runner = SecureToolRunner()
        self.timeout = timeout

    def run_checks(self, code_path: Path, step_id: str) -> dict:
        """
        对 project 代码仓库执行构建和测试检查。

        :param code_path: 项目代码仓库路径
        :param step_id: 当前步骤 ID (用于决定检查策略)
        :return: {"passed": bool, "checks": [...], "summary": str}
        """
        project_dir = code_path
        if not project_dir.exists():
            return {"passed": True, "checks": [], "summary": "No project directory, skipping build checks"}

        languages = self._detect_languages(project_dir)

        if not languages:
            return {"passed": True, "checks": [], "summary": "No recognizable language files, skipping build checks"}

        checks = []

        if "python" in languages:
            checks.extend(self._check_python(project_dir))

        if "node" in languages:
            checks.extend(self._check_node(project_dir))

        passed = all(c["passed"] for c in checks)
        summary = self._build_summary(checks)

        return {"passed": passed, "checks": checks, "summary": summary}

    def _detect_languages(self, project_dir: Path) -> set[str]:
        """检测项目使用的语言/运行时。"""
        languages = set()

        # Python detection
        py_files = list(project_dir.rglob("*.py"))
        if py_files or (project_dir / "pyproject.toml").exists() or (project_dir / "setup.py").exists():
            languages.add("python")

        # Node detection
        if (project_dir / "package.json").exists():
            languages.add("node")

        return languages

    def _check_python(self, project_dir: Path) -> list[dict]:
        """对 Python 项目执行编译检查 + 测试。"""
        checks = []

        # 1. Compile check — verify all .py files have valid syntax
        py_files = [f for f in project_dir.rglob("*.py")
                    if f.name != "__init__.py" or f.stat().st_size > 0]

        compile_errors = []
        for py_file in py_files:
            result = self.runner.run_cmd(
                project_dir,
                [sys.executable, "-m", "py_compile", str(py_file.relative_to(project_dir))],
                timeout=30,
                use_mise=False,
            )
            if result["exit_code"] != 0:
                compile_errors.append(
                    f"{py_file.relative_to(project_dir)}: {result['stdout_text']}"
                )

        checks.append({
            "name": "python_compile",
            "passed": len(compile_errors) == 0,
            "output": "\n".join(compile_errors) if compile_errors else f"All {len(py_files)} Python file(s) compiled successfully",
        })

        # 2. Run pytest if tests exist
        test_dirs = list(project_dir.rglob("test_*"))
        test_files = list(project_dir.rglob("test_*.py")) + list(project_dir.rglob("*_test.py"))

        if test_files or any(f.is_dir() for f in test_dirs):
            # Clean __pycache__ directories to prevent stale module import conflicts
            for pycache in project_dir.rglob("__pycache__"):
                if pycache.is_dir():
                    shutil.rmtree(pycache, ignore_errors=True)

            # Build PYTHONPATH: include project root + all artifact subdirs
            # so tests can import modules from sibling artifact directories
            python_paths = [str(project_dir)]
            for artifact_dir in project_dir.rglob("artifacts/*"):
                if artifact_dir.is_dir():
                    python_paths.append(str(artifact_dir))
            test_env = {"PYTHONPATH": os.pathsep.join(python_paths)}

            result = self.runner.run_cmd(
                project_dir,
                [sys.executable, "-m", "pytest", "-x", "--tb=short", "-q",
                 "--import-mode=importlib", "--rootdir", str(project_dir)],
                timeout=self.timeout,
                use_mise=False,
                env=test_env,
            )
            checks.append({
                "name": "python_tests",
                "passed": result["exit_code"] == 0,
                "output": result["stdout_text"],
            })
        else:
            checks.append({
                "name": "python_tests",
                "passed": True,
                "output": "No test files found, skipping test run",
            })

        return checks

    def _check_node(self, project_dir: Path) -> list[dict]:
        """对 Node 项目执行构建 + 测试。"""
        checks = []

        # 1. npm install + build
        if (project_dir / "package.json").exists():
            # Install dependencies
            install_result = self.runner.run_cmd(
                project_dir,
                ["npm", "install"],
                timeout=self.timeout,
                use_mise=False,
            )
            checks.append({
                "name": "node_install",
                "passed": install_result["exit_code"] == 0,
                "output": install_result["stdout_text"],
            })

            # Run build if script exists
            pkg = _read_package_json(project_dir)
            if pkg and "build" in pkg.get("scripts", {}):
                build_result = self.runner.run_cmd(
                    project_dir,
                    ["npm", "run", "build"],
                    timeout=self.timeout,
                    use_mise=False,
                )
                checks.append({
                    "name": "node_build",
                    "passed": build_result["exit_code"] == 0,
                    "output": build_result["stdout_text"],
                })

            # Run tests if script exists
            if pkg and "test" in pkg.get("scripts", {}):
                test_result = self.runner.run_cmd(
                    project_dir,
                    ["npm", "test"],
                    timeout=self.timeout,
                    use_mise=False,
                )
                checks.append({
                    "name": "node_tests",
                    "passed": test_result["exit_code"] == 0,
                    "output": test_result["stdout_text"],
                })

        return checks

    def _build_summary(self, checks: list[dict]) -> str:
        """构建人类可读的检查摘要。"""
        if not checks:
            return "No checks run"

        parts = []
        for c in checks:
            status = "OK" if c["passed"] else "FAILED"
            parts.append(f"{c['name']}: {status}")
        return " | ".join(parts)


def _read_package_json(project_dir: Path) -> dict | None:
    """读取 package.json，失败返回 None。"""
    pkg_path = project_dir / "package.json"
    if not pkg_path.exists():
        return None
    import json
    try:
        return json.loads(pkg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
