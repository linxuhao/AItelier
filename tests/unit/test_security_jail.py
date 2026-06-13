# File: tests/test_security_jail.py

import pytest
from pathlib import Path
from core.security_jail import SecurityException, verify_path_safe

def test_verify_path_safe_valid(tmp_path: Path):
    """测试合法路径能否被正确解析和放行"""
    workspace_root = tmp_path / "sandbox"
    workspace_root.mkdir()
    
    # 1. 基础相对路径
    safe_1 = verify_path_safe(workspace_root, "inbox/test.py")
    assert safe_1 == workspace_root / "inbox" / "test.py"
    
    # 2. 带有同级折返的合法相对路径
    safe_2 = verify_path_safe(workspace_root, "inbox/../outbox/data.json")
    assert safe_2 == workspace_root / "outbox" / "data.json"
    
    # 3. 在沙盒范围内的绝对路径
    absolute_safe = workspace_root / "outbox_draft" / "draft.txt"
    safe_3 = verify_path_safe(workspace_root, absolute_safe)
    assert safe_3 == absolute_safe

def test_verify_path_safe_traversal_blocks(tmp_path: Path):
    """测试各种路径穿越漏洞 (Path Traversal) 能否被精准拦截"""
    workspace_root = tmp_path / "sandbox"
    workspace_root.mkdir()
    
    # 1. 尝试使用 ../ 逃逸到上一级目录
    with pytest.raises(SecurityException) as excinfo:
        verify_path_safe(workspace_root, "../../etc/passwd")
    assert "Path Traversal Attempt Detected" in str(excinfo.value)
    
    # 2. 尝试使用深层级逃逸
    with pytest.raises(SecurityException):
        verify_path_safe(workspace_root, "inbox/../../../../shadow")
        
    # 3. 尝试直接传递工作区外部的绝对路径
    with pytest.raises(SecurityException):
        verify_path_safe(workspace_root, "/tmp/malicious_payload.sh")