"""skillflow_docs_search — grep skillflow docs/schema, line-numbered snippets."""
from aitelier.skillflow_docs_lib import search_docs


def skillflow_docs_search(query: str = "", **kwargs) -> dict:
    return search_docs(query)
