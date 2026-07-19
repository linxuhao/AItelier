"""skillflow_docs_list — enumerate skillflow doc topics."""
from aitelier.skillflow_docs_lib import list_topics


def skillflow_docs_list(**kwargs) -> dict:
    return list_topics()
