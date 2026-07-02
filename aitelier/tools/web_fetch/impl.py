"""Web fetch tool — delegates to core/web_tools.py WebFetchTool."""

import sys
from pathlib import Path


def web_fetch(url: str, offset: int = 0, *, workspace_root: str = "") -> dict:
    project_root = Path(__file__).parent.parent.parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    from core.web_tools import WebFetchTool
    tool = WebFetchTool()
    try:
        offset = int(offset or 0)
    except (TypeError, ValueError):
        offset = 0
    result = tool.fetch(url, offset=offset)
    return result
