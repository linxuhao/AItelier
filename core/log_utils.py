# core/log_utils.py
# [修复说明] 在现有的 truncate_logs 基础上，补充了 MVP 5.1 要求的 build_error_context 组装函数。

def truncate_logs(raw_log: str, max_chars: int = 2000) -> str:
    """
    截断过长日志，保留首尾以供大模型分析，防止死循环报错耗尽 Token。
    :param raw_log: 原始终端 stderr/stdout
    :param max_chars: 最大允许字符数 (默认 2000)
    :return: 截断后的安全日志文本
    """
    if not raw_log or len(raw_log) <= max_chars:
        return raw_log
    
    head_len = 500
    tail_len = max_chars - head_len
    separator = "\n...[TRUNCATED]...\n"
    
    return f"{raw_log[:head_len]}{separator}{raw_log[-tail_len:]}"

def build_error_context(draft_code: str, stderr: str) -> str:
    """
    将草案代码与截断后的报错日志拼装为清晰的 Markdown 块。
    """
    truncated_stderr = truncate_logs(stderr)
    
    return (
        "### Draft Code\n"
        "```python\n"
        f"{draft_code}\n"
        "```\n\n"
        "### Execution Error\n"
        "```text\n"
        f"{truncated_stderr}\n"
        "```"
    )