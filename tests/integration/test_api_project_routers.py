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
