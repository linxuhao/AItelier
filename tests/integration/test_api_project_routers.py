# tests/integration/test_api_project_routers.py
# Integration tests for api/project_routers.py

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


def _owner_of(client: TestClient, project_id: str) -> str:
    row = next(p for p in client.get("/api/projects").json()
               if p["project_id"] == project_id)
    return row["owner_email"]


class TestOwnerAttribution:
    def test_owner_defaults_to_cli_local_without_cf(self, client: TestClient):
        # No Cloudflare Access header → genuine localhost CLI → cli@local.
        client.post("/api/projects", json={"project_id": "own_a", "name": "A"})
        assert _owner_of(client, "own_a") == "cli@local"

    def test_owner_is_verified_cf_access_email(self, client: TestClient):
        # A verified Access JWT on the tunnel path → the requester is the owner.
        with patch("core.cf_access.email_from_request_headers",
                   return_value="alice@example.com"):
            client.post("/api/projects", json={"project_id": "own_b", "name": "B"})
        assert _owner_of(client, "own_b") == "alice@example.com"


class TestProjectAPI:
    def test_create_project(self, client: TestClient):
        resp = client.post("/api/projects", json={
            "project_id": "test_proj",
            "name": "Test Project",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["project_id"] == "test_proj"
        assert data["name"] == "Test Project"

    def test_create_project_records_its_pipeline_at_creation(self, client: TestClient,
                                                             db_manager):
        """A project must know its pipeline the moment the row exists.

        The scheduler resolves a project's run by `config_name`
        (core/scheduler.py:_get_or_create_skillflow_run), falling back to
        "dpe_default_v2" while the column is unset — and the poller ticks every
        five seconds. Live 2026-08-24: a game project created at 01:36:43 had a
        dpe_default_v2 run auto-created at 01:36:44, six seconds before the
        intended dpe_game run reached the API. dpe_default_v2 carries no
        game_harness overlay, so that round had no compile, play-test or vision
        step at all and would have finished green over an unverified game.
        """
        resp = client.post("/api/projects", json={
            "project_id": "game_proj",
            "name": "Game Project",
            "config_name": "dpe_game",
        })
        assert resp.status_code == 201, resp.text
        assert db_manager.get_project("game_proj")["config_name"] == "dpe_game"

    def test_create_project_defaults_to_the_dpe_pipeline(self, client: TestClient,
                                                         db_manager):
        """Omitting it keeps the column's existing default — not None."""
        client.post("/api/projects", json={"project_id": "plain_proj"})
        assert db_manager.get_project("plain_proj")["config_name"] == "dpe_default_v2"

    def test_create_project_duplicate_409(self, client: TestClient):
        client.post("/api/projects", json={
            "project_id": "dup_proj",
            "name": "First",
        })
        resp = client.post("/api/projects", json={
            "project_id": "dup_proj",
            "name": "Second",
        })
        assert resp.status_code == 409

    def test_list_projects(self, client: TestClient):
        client.post("/api/projects", json={"project_id": "proj_a", "name": "A"})
        client.post("/api/projects", json={"project_id": "proj_b", "name": "B"})

        resp = client.get("/api/projects")
        assert resp.status_code == 200
        data = resp.json()
        ids = {p["project_id"] for p in data}
        assert "proj_a" in ids
        assert "proj_b" in ids

    def test_get_project(self, client: TestClient):
        client.post("/api/projects", json={"project_id": "proj_x", "name": "X"})
        resp = client.get("/api/projects/proj_x")
        assert resp.status_code == 200
        assert resp.json()["project_id"] == "proj_x"

    def test_get_project_not_found(self, client: TestClient):
        resp = client.get("/api/projects/nonexistent")
        assert resp.status_code == 404

    def test_delete_project(self, client: TestClient):
        client.post("/api/projects", json={"project_id": "to_delete", "name": "Del"})
        resp = client.delete("/api/projects/to_delete")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

        # Verify it's gone
        resp = client.get("/api/projects/to_delete")
        assert resp.status_code == 404

    def test_delete_project_not_found(self, client: TestClient):
        resp = client.delete("/api/projects/nonexistent")
        assert resp.status_code == 404

    def test_create_project_default_name(self, client: TestClient):
        """Project name should default to title-cased project_id."""
        resp = client.post("/api/projects", json={"project_id": "my-cool-app"})
        assert resp.status_code == 201
        assert resp.json()["name"] == "My Cool App"

    def test_create_project_with_repo_type_new(self, client: TestClient):
        """Project creation with repo_type='new'."""
        resp = client.post("/api/projects", json={
            "project_id": "new_repo_proj",
            "name": "New Repo",
            "repo_type": "new",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["repo_type"] == "new"
        assert data["repo_path"] is not None

    def test_create_project_existing_missing_path_400(self, client: TestClient):
        """repo_type='existing' without repo_path should return 400."""
        resp = client.post("/api/projects", json={
            "project_id": "bad_existing",
            "repo_type": "existing",
        })
        assert resp.status_code == 400

    def test_create_project_clone_missing_url_400(self, client: TestClient):
        """repo_type='clone' without repo_url should return 400."""
        resp = client.post("/api/projects", json={
            "project_id": "bad_clone",
            "repo_type": "clone",
        })
        assert resp.status_code == 400

    def test_create_project_existing_not_git_400(self, client: TestClient, tmp_path):
        """repo_type='existing' with non-git path should return 400."""
        not_git = tmp_path / "not_git"
        not_git.mkdir()

        # Need to pass the path through the test client
        resp = client.post("/api/projects", json={
            "project_id": "not_git_proj",
            "repo_type": "existing",
            "repo_path": str(not_git),
        })
        assert resp.status_code == 400


class TestWorkspaceFilePaging:
    """workspace_file endpoint: line paging replaces silent 50000-char cut."""

    def _make_project_file(self, client, tmp_path, pid, name, body):
        client.post("/api/projects", json={"project_id": pid, "name": pid})
        # dps root resolves to <ws_base>/<project_id> (see _get_secure_path)
        fp = tmp_path / "ws" / pid / name
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(body)
        return fp

    def test_small_file_whole_not_truncated(self, client: TestClient, tmp_path):
        self._make_project_file(client, tmp_path, "fp_small", "a.txt", "L1\nL2\nL3")
        resp = client.get("/api/projects/fp_small/workspace/file", params={"path": "a.txt"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_lines"] == 3
        assert data["truncated"] is False
        assert data["content"] == "L1\nL2\nL3"  # raw, no line-number prefix

    def test_large_file_paged_and_flagged(self, client: TestClient, tmp_path):
        body = "\n".join(f"line{i}" for i in range(1, 5001))
        self._make_project_file(client, tmp_path, "fp_big", "big.txt", body)
        resp = client.get("/api/projects/fp_big/workspace/file", params={"path": "big.txt"})
        data = resp.json()
        assert data["total_lines"] == 5000
        assert data["truncated"] is True
        assert data["end_line"] == 2000

    def test_explicit_range(self, client: TestClient, tmp_path):
        body = "\n".join(f"line{i}" for i in range(1, 101))
        self._make_project_file(client, tmp_path, "fp_range", "big.txt", body)
        resp = client.get("/api/projects/fp_range/workspace/file",
                          params={"path": "big.txt", "start_line": 10, "end_line": 12})
        data = resp.json()
        assert data["start_line"] == 10 and data["end_line"] == 12
        assert data["content"] == "line10\nline11\nline12"
        assert data["truncated"] is True


class TestWorkspaceRawImage:
    """workspace/raw serves image bytes — workspace/file only ever yields text."""

    # Smallest valid PNG (1x1, transparent).
    _PNG = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c6360000002000100055c5b0e0000000049454e44ae426082"
    )

    def _make(self, client, tmp_path, pid, name, blob: bytes):
        client.post("/api/projects", json={"project_id": pid, "name": pid})
        fp = tmp_path / "ws" / pid / name
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_bytes(blob)
        return fp

    def test_png_served_with_image_content_type(self, client: TestClient, tmp_path):
        self._make(client, tmp_path, "raw_png", "shot.png", self._PNG)
        resp = client.get("/api/projects/raw_png/workspace/raw",
                          params={"path": "shot.png"})
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"
        assert resp.content == self._PNG
        # Agent-produced bytes: pinned type, and nothing of their own may run.
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert "default-src 'none'" in resp.headers["content-security-policy"]

    def test_non_image_rejected_415(self, client: TestClient, tmp_path):
        self._make(client, tmp_path, "raw_txt", "a.txt", b"hello")
        resp = client.get("/api/projects/raw_txt/workspace/raw",
                          params={"path": "a.txt"})
        assert resp.status_code == 415

    def test_path_traversal_denied(self, client: TestClient, tmp_path):
        self._make(client, tmp_path, "raw_trav", "shot.png", self._PNG)
        resp = client.get("/api/projects/raw_trav/workspace/raw",
                          params={"path": "../raw_trav_evil/x.png"})
        assert resp.status_code in (403, 404)

    def test_missing_image_404(self, client: TestClient, tmp_path):
        self._make(client, tmp_path, "raw_missing", "shot.png", self._PNG)
        resp = client.get("/api/projects/raw_missing/workspace/raw",
                          params={"path": "nope.png"})
        assert resp.status_code == 404
