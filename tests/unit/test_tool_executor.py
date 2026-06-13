# File: tests/test_tool_executor.py

import time
import pytest
from pathlib import Path
from core.tool_executor import SecureToolRunner

def test_secure_tool_runner_success(tmp_path: Path):
    """测试常规指令的执行与输出捕获"""
    runner = SecureToolRunner()
    
    # use_mise=False 避免本地没有安装 mise 导致测试报错
    result = runner.run_cmd(
        workspace=tmp_path, 
        cmd=["echo", "deterministic_test"], 
        use_mise=False
    )
    
    assert result["exit_code"] == 0
    assert result["timeout"] is False
    assert result["stdout_text"] == "deterministic_test"

def test_secure_tool_runner_timeout_harvesting(tmp_path: Path):
    """测试死循环/超时任务能否被 SIGKILL 准确连根拔起"""
    runner = SecureToolRunner()
    
    start_time = time.time()
    
    # 设定任务需要睡眠 5 秒，但沙盒只给 1 秒寿命
    result = runner.run_cmd(
        workspace=tmp_path,
        cmd=["sleep", "5"],
        timeout=1,
        use_mise=False
    )
    
    elapsed = time.time() - start_time
    
    # 断言：超时拦截必须在 1 秒多一点点的时间内完成，绝不能拖延到 5 秒
    assert elapsed < 2.0
    
    # 断言：状态码必须为 SIGKILL (-9) 或明确标识为 Timeout
    assert result["timeout"] is True
    assert result["exit_code"] == -9
    assert "[DPE Engine] Process killed" in result["stdout_text"]

def test_secure_tool_runner_invalid_cmd(tmp_path: Path):
    """测试执行不存在的命令时的容错性"""
    runner = SecureToolRunner()
    result = runner.run_cmd(
        workspace=tmp_path,
        cmd=["non_existent_command_12345"],
        use_mise=False
    )
    
    assert result["exit_code"] == -1
    assert "No such file or directory" in result["stdout_text"] or "FileNotFoundError" in result["stdout_text"]