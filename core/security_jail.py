# File: core/security_jail.py

from pathlib import Path

class SecurityException(Exception):
    """当检测到路径越权 (Path Traversal) 尝试时抛出的安全隔离异常"""
    pass

def verify_path_safe(workspace_root: Path | str, target_path: Path | str) -> Path:
    """
    解析并验证目标路径是否被严格限制在工作区根目录范围之内。
    如果检测到试图通过 `../` 逃逸或直接访问沙盒外部绝对路径，则抛出 SecurityException。
    """
    root = Path(workspace_root).resolve()
    target = Path(target_path)
    
    # 若传入的是纯相对路径，将其绑定到工作区根目录后再行解析
    if not target.is_absolute():
        target = root / target
        
    resolved_target = target.resolve()
    
    # 判断解析后的绝对物理路径是否处于根目录之下
    if not resolved_target.is_relative_to(root):
        raise SecurityException(
            f"Path Traversal Attempt Detected! Target '{resolved_target}' "
            f"is outside the secured workspace '{root}'"
        )
    
    return resolved_target