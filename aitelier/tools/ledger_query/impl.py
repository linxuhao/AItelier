"""ledger_query — look up the append-only novel ledger (query-only history).

Bible cards are CURRENT STATE; every per-chapter change lives in
``novel/ledger/<kind>/<entity>.jsonl``. This tool filters those rows by entity
and chapter so an agent can answer "尹骁第 8 章右臂怎样了" without the whole
history ever entering the drafting context.
"""

import json
from pathlib import Path

from aitelier import novel_state as ns

MAX_ROWS = 200


def ledger_query(*, project_root: str = "", workspace_root: str = "",
                 kind: str = "", name: str = "", chapter: int | None = None,
                 chapter_from: int | None = None, chapter_to: int | None = None,
                 field: str = "", entry_type: str = "", limit: int = 50,
                 out_dir: str = "", **kwargs) -> dict:
    base = project_root or workspace_root
    if not base or not Path(base).is_absolute():
        raise ValueError("ledger_query: project_root/workspace_root must be an "
                         "absolute path")
    if kind and kind not in ns.LEDGER_KINDS:
        raise ValueError(f"ledger_query: kind must be one of {ns.LEDGER_KINDS}")
    rows = ns.query_ledger(
        base, kind=kind or None, name=name or None, chapter=chapter,
        chapter_from=chapter_from, chapter_to=chapter_to, field=field or None,
        entry_type=entry_type or None,
        limit=min(int(limit or MAX_ROWS), MAX_ROWS))
    result = {"count": len(rows), "rows": rows}
    if out_dir:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "ledger_query.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
