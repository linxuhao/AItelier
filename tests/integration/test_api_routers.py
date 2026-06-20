# tests/integration/test_api_routers.py

import pytest
from fastapi.testclient import TestClient


def test_health_check(client: TestClient):
    """测试 FastAPI 挂载是否正常"""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

def test_create_and_get_task(client: TestClient):
    """测试流水线 API 任务创建与数据回查闭环"""
    # 1. 创建任务 (project must exist first — /api/tasks does not auto-create)
    client.post("/api/projects", json={"project_id": "api_test_proj"})
    payload = {"project_id": "api_test_proj", "prompt": "Scrape github"}
    response = client.post("/api/tasks", json=payload)

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["project_id"] == "api_test_proj"
    assert data["status"] == "pending"
    task_id = data["id"]

    # 2. 查询任务
    get_res = client.get(f"/api/tasks/{task_id}")
    assert get_res.status_code == 200
    assert get_res.json()["id"] == task_id

def test_rollback_task_not_found(client: TestClient):
    """测试 404 容错"""
    response = client.post("/api/tasks/999/rollback", json={"commit_hash": "abcd"})
    assert response.status_code == 404

def test_list_tasks_empty(client: TestClient):
    """空数据库应该返回空列表"""
    response = client.get("/api/tasks")
    assert response.status_code == 200
    assert response.json() == []

def test_get_task_not_found(client: TestClient):
    """查询不存在的任务应返回 404"""
    response = client.get("/api/tasks/99999")
    assert response.status_code == 404

def test_create_task_with_brief(client: TestClient):
    """创建任务时附带 project_brief 应写入 workspace"""
    client.post("/api/projects", json={"project_id": "brief_proj"})
    payload = {
        "project_id": "brief_proj",
        "prompt": "Build something",
        "project_brief": "# Project Brief\nThis is a test brief.",
    }
    response = client.post("/api/tasks", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["project_id"] == "brief_proj"
