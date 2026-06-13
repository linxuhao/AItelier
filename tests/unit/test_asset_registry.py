# tests/test_asset_registry.py
# [说明] 验证跨域拷贝、执行权限(chmod 755)赋予以及 JSON Manifest 的更新。

import os
import json
import pytest
from pathlib import Path
from core.asset_registry import register_tool

@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """
    利用 monkeypatch 将 Path.home() 劫持到 tmp_path/home，
    防止测试脚本弄脏宿主机真实的 ~/.local 目录。
    """
    mock_home = tmp_path / "home"
    mock_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: mock_home)
    return mock_home

def test_register_tool_success(tmp_path, isolated_home):
    """
    测试: 正常注册流程，文件能被成功拷贝，赋权并更新 manifest。
    """
    workspace = tmp_path / "workspace"
    outbox = workspace / "5"
    outbox.mkdir(parents=True)
    
    # 构造假产物
    script_file = outbox / "main.py"
    script_file.write_text("print('Tool Ready')", encoding="utf-8")
    
    config_file = outbox / ".mise.toml"
    config_file.write_text("[env]\nDEBUG=1", encoding="utf-8")
    
    # 执行注册
    result = register_tool(workspace, tool_name="data_analyzer")
    
    assert result is True
    
    # 断言系统级目录已被生成
    target_tool_dir = isolated_home / ".local" / "share" / "aitelier_tools" / "data_analyzer"
    assert target_tool_dir.exists()
    
    # 断言文件被成功迁移
    assert (target_tool_dir / "main.py").exists()
    assert (target_tool_dir / ".mise.toml").exists()
    
    # 断言执行权限 (0o755 => 可执行)
    assert os.access(target_tool_dir / "main.py", os.X_OK)
    
    # 断言 Manifest 注册表已生成并包含正确的元数据
    manifest_path = isolated_home / ".local" / "share" / "aitelier_tools" / "manifest.json"
    assert manifest_path.exists()
    
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
        assert len(manifest) == 1
        assert manifest[0]["name"] == "data_analyzer"
        assert manifest[0]["path"] == str(target_tool_dir)
        assert "updated_at" in manifest[0]

def test_register_tool_missing_outbox(tmp_path):
    """
    测试: 拦截异常情况，若 Final Outbox 不存在直接报错，避免注册空工具。
    """
    workspace = tmp_path / "empty_workspace"
    
    with pytest.raises(FileNotFoundError, match="Source step dir not found"):
        register_tool(workspace, tool_name="ghost_tool")