"""Web search tool — delegates to core/web_tools.py WebSearchTool."""

import sys
from pathlib import Path

# Import from core/web_tools (lazy to avoid circular imports at module level)
def web_search(query: str, *, workspace_root: str = "") -> dict:
    # Add project root to path
    project_root = Path(__file__).parent.parent.parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    from core.web_tools import WebSearchTool
    tool = WebSearchTool()
    result = tool.search(query)
    return result
