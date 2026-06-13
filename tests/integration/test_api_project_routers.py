# tests/integration/test_api_project_routers.py
# Integration tests for api/project_routers.py

import pytest
from fastapi.testclient import TestClient


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
