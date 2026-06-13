# core/asset_registry.py
# [说明] 将五步法跑通的临时工作区代码，提纯、赋权并沉淀为全局可复用的二进制/脚本资产。

import os
import json
import shutil
from pathlib import Path
from datetime import datetime, timezone

def register_tool(workspace: Path, tool_name: str, outbox_name: str = "5") -> bool:
    """
    将 step output 目录的产物注入系统路径，并登记 Manifest。

    :param workspace: graph config 目录 (e.g. .../dpe_default_v2/)
    :param tool_name: 要注册的工具全局命名
    :param outbox_name: step output 目录名称 (默认 step 5)
    """
    source_dir = workspace / outbox_name

    if not source_dir.exists() or not source_dir.is_dir():
        raise FileNotFoundError(f"Source step dir not found: {source_dir}")

    # 定义系统级目标存储路径: ~/.local/share/aitelier_tools/
    sys_tools_dir = Path.home() / ".local" / "share" / "aitelier_tools"
    target_dir = sys_tools_dir / tool_name
    manifest_path = sys_tools_dir / "manifest.json"

    # 1. 创建目标资产目录
    target_dir.mkdir(parents=True, exist_ok=True)

    # 2. 物理拷贝与权限重置 (赋权可执行)
    for item in source_dir.iterdir():
        dest_path = target_dir / item.name
        if item.is_file():
            shutil.copy2(item, dest_path)
            # 针对脚本文件强制赋予 chmod +x 权限，以便后续通过 mise exec 甚至原生 bash 快速拉起
            if item.suffix in ['.py', '.sh', '.js', '.ts'] or not item.suffix:
                os.chmod(dest_path, 0o755)
        elif item.is_dir():
            shutil.copytree(item, dest_path, dirs_exist_ok=True)

    # 3. 追加元数据至全局 Manifest 注册表
    manifest = []
    if manifest_path.exists():
        with open(manifest_path, "r", encoding="utf-8") as f:
            try:
                manifest = json.load(f)
            except json.JSONDecodeError:
                pass

    # 若已存在同名工具，剔除旧记录
    manifest = [t for t in manifest if t.get("name") != tool_name]

    # 追加新记录
    manifest.append({
        "name": tool_name,
        "path": str(target_dir),
        "updated_at": datetime.now(timezone.utc).isoformat()
    })

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    return True