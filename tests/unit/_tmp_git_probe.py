import subprocess
from pathlib import Path


def test_report_git_state():
    root = Path(__file__).resolve().parents[2]
    status = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                            capture_output=True, text=True, check=True)
    diff = subprocess.run(
        ["git", "diff", "--", "core/", "templates/", "tests/fixtures/"],
        cwd=root, capture_output=True, text=True, check=True)
    raise AssertionError("STATUS:\n" + status.stdout + "DIFF:\n" + diff.stdout)
