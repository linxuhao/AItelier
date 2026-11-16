"""dsh_todo: todo plans whose completion is observed, not claimed (`python -m dsh_todo --help`).

cli (the JSON command line), plan (the plan store), runtime (predicates and evaluation),
templates (reviewed plan templates), batch (find/verify batches). The host supplies its policy
through hooks.py; nothing here imports the host.
"""

#: The module that supplies the host hooks (see hooks.py), for a host that imports dsh_todo
#: in-process: set it before importing any other dsh_todo module. DSH_TODO_HOOKS, when set,
#: takes precedence (that is how a host configures `python -m dsh_todo`).
HOOKS: str | None = None
