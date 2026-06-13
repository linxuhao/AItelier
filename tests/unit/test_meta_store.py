# tests/unit/test_meta_store.py
# Tests for cli/meta_store.py — file-based meta conversation persistence.

import json
import pytest
from pathlib import Path
from unittest.mock import patch

from cli.meta_store import (
    save_project_meta, load_project_meta, clear_project_meta,
    save_task_meta, load_task_meta, clear_task_meta,
    list_pending_task_metas,
)


@pytest.fixture(autouse=True)
def tmp_meta_dir(tmp_path):
    """Redirect meta storage to a temp directory."""
    with patch("cli.meta_store._META_DIR", tmp_path / "meta"):
        yield tmp_path / "meta"


class TestProjectMeta:
    def test_save_and_load(self, tmp_meta_dir):
        state = {"prompt": "build app", "history": [], "status": "asking"}
        save_project_meta("my-proj", state)
        loaded = load_project_meta("my-proj")
        assert loaded["prompt"] == "build app"
        assert loaded["status"] == "asking"
        assert loaded["type"] == "project"

    def test_load_nonexistent(self, tmp_meta_dir):
        assert load_project_meta("no-such-proj") is None

    def test_clear(self, tmp_meta_dir):
        save_project_meta("my-proj", {"status": "asking"})
        assert load_project_meta("my-proj") is not None
        clear_project_meta("my-proj")
        assert load_project_meta("my-proj") is None

    def test_clear_nonexistent(self, tmp_meta_dir):
        clear_project_meta("no-such-proj")  # should not raise


class TestTaskMeta:
    def test_save_and_load(self, tmp_meta_dir):
        state = {"prompt": "add login", "history": [], "status": "asking"}
        save_task_meta("my-proj", 42, state)
        loaded = load_task_meta("my-proj", 42)
        assert loaded["prompt"] == "add login"
        assert loaded["task_id"] == 42
        assert loaded["type"] == "task"

    def test_load_nonexistent(self, tmp_meta_dir):
        assert load_task_meta("my-proj", 99) is None

    def test_clear(self, tmp_meta_dir):
        save_task_meta("my-proj", 42, {"status": "asking"})
        clear_task_meta("my-proj", 42)
        assert load_task_meta("my-proj", 42) is None


class TestListPending:
    def test_lists_asking_only(self, tmp_meta_dir):
        save_task_meta("my-proj", 1, {"status": "asking", "prompt": "A"})
        save_task_meta("my-proj", 2, {"status": "complete", "prompt": "B"})
        save_task_meta("my-proj", 3, {"status": "asking", "prompt": "C"})

        pending = list_pending_task_metas("my-proj")
        assert len(pending) == 2
        prompts = [p["prompt"] for p in pending]
        assert "A" in prompts
        assert "C" in prompts

    def test_empty(self, tmp_meta_dir):
        assert list_pending_task_metas("my-proj") == []

    def test_different_projects(self, tmp_meta_dir):
        save_task_meta("proj-a", 1, {"status": "asking", "prompt": "A"})
        save_task_meta("proj-b", 2, {"status": "asking", "prompt": "B"})

        assert len(list_pending_task_metas("proj-a")) == 1
        assert len(list_pending_task_metas("proj-b")) == 1
