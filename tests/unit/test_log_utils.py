# tests/test_log_utils.py

import pytest
from core.log_utils import truncate_logs, build_error_context

def test_truncate_logs_short_string():
    """测试: 日志较短时原样返回"""
    raw_log = "A" * 1500
    assert truncate_logs(raw_log) == raw_log

def test_truncate_logs_long_string():
    """测试: 日志超出限制时，正确截断并保留首尾特征"""
    # 构造超出 max_chars (2000) 的字符串
    raw_log = ("H" * 500) + ("M" * 3000) + ("T" * 1500)
    
    result = truncate_logs(raw_log)
    
    # 断言中间插入了截断标识
    assert "\n...[TRUNCATED]...\n" in result
    
    # 断言首部 500 字符未丢失
    assert result.startswith("H" * 500)
    
    # 断言尾部 1500 字符未丢失
    assert result.endswith("T" * 1500)
    
    # 断言被截断掉的字符 'M' 不存在于最终结果中
    assert "M" not in result
    
    # 断言总长度符合预期
    expected_length = 2000 + len("\n...[TRUNCATED]...\n")
    assert len(result) == expected_length

def test_build_error_context():
    """测试: Markdown 报错上下文的成功拼接"""
    draft_code = "print(1 / 0)"
    stderr = "ZeroDivisionError: division by zero"
    
    result = build_error_context(draft_code, stderr)
    
    assert "### Draft Code" in result
    assert draft_code in result
    assert "### Execution Error" in result
    assert stderr in result