# External/internal exposure of the novel ledger query:
#   HTTP  GET /api/projects/{pid}/novel/ledger   (require_reader)
#   MCP   novel_ledger_query                      (private read)
#   skillflow tool ledger_query on the novel agent roles

import asyncio
from pathlib import Path

import pytest
import yaml

from aitelier import novel_state as ns
from api import mcp_router
from api.novel_routers import LedgerLookupError, ledger_lookup

ROOT = Path(__file__).resolve().parents[2]


def _novel(root: Path):
    b = ns.bible_dir(root)
    b.mkdir(parents=True, exist_ok=True)
    (b / "overview.md").write_text("# 总纲", encoding="utf-8")
    ns.dump_yaml(ns.character_path(root, "尹骁"), {"name": "尹骁", "status": "alive"})
    for ch, changes in [(3, {"右臂": "红"}), (8, {"右臂伤": "白"}), (9, {"气": "低"})]:
        ns.apply_events(root, [{"entity_type": "character", "entity_name": "尹骁",
                                "changes": changes, "reason": f"r{ch}"}], ch)


class _DB:
    def __init__(self, ids):
        self.ids = set(ids)

    def get_project(self, pid):
        return {"id": pid} if pid in self.ids else None


class _WS:
    def __init__(self, base):
        self.base = Path(base)

    def get_code_path(self, pid, run_id=None):
        return self.base / pid


def test_lookup_filters_and_refusals(tmp_path):
    _novel(tmp_path / "p1")
    (tmp_path / "p2").mkdir()
    db, ws = _DB({"p1", "p2"}), _WS(tmp_path)
    out = ledger_lookup("p1", db=db, ws=ws, kind="characters", name="尹骁",
                        field="右臂", chapter_from=5)
    assert [r["chapter"] for r in out["rows"]] == [8]
    assert out["rows"][0]["changes"] == {"右臂伤": "白"}
    with pytest.raises(LedgerLookupError) as e:
        ledger_lookup("nope", db=db, ws=ws)
    assert e.value.status == 404
    with pytest.raises(LedgerLookupError) as e:
        ledger_lookup("p2", db=db, ws=ws)              # no novel bible
    assert e.value.status == 404
    with pytest.raises(LedgerLookupError) as e:
        ledger_lookup("p1", db=db, ws=ws, kind="bogus")
    assert e.value.status == 400
    with pytest.raises(LedgerLookupError):
        ledger_lookup("../etc", db=db, ws=ws)
    assert ledger_lookup("p1", db=db, ws=ws, limit=10_000)["limit"] == 200


def test_http_route(client, tmp_path):
    from api.dependencies import get_db_manager, get_workspace_manager
    from api.main import app
    _novel(tmp_path / "px")
    app.dependency_overrides[get_db_manager] = lambda: _DB({"px"})
    app.dependency_overrides[get_workspace_manager] = lambda: _WS(tmp_path)
    r = client.get("/api/projects/px/novel/ledger",
                   params={"kind": "characters", "name": "尹骁", "chapter": 9})
    assert r.status_code == 200, r.text
    assert r.json()["rows"][0]["changes"] == {"气": "低"}
    assert client.get("/api/projects/missing/novel/ledger").status_code == 404
    assert client.get("/api/projects/px/novel/ledger",
                      params={"kind": "bogus"}).status_code == 400


def test_http_route_is_a_private_read():
    from api.novel_routers import router
    route = next(r for r in router.routes
                 if getattr(r, "path", "") == "/api/projects/{project_id}/novel/ledger")
    deps = [d.call for d in route.dependant.dependencies]
    from api import authz
    assert authz.require_reader in deps


def test_mcp_tool_is_registered_as_private_read(monkeypatch, tmp_path):
    mcp = mcp_router.build_mcp()
    assert mcp_router._TOOL_KIND["novel_ledger_query"] == "read"
    assert "novel_ledger_query" in mcp_router._PRIVATE_READ_TOOLS
    _novel(tmp_path / "pm")
    import api.dependencies as deps
    monkeypatch.setattr(deps, "get_db_manager", lambda: _DB({"pm"}))
    monkeypatch.setattr(deps, "get_workspace_manager", lambda: _WS(tmp_path))
    fn = mcp._tool_manager.get_tool("novel_ledger_query").fn
    monkeypatch.setattr(mcp, "get_context", lambda: object())
    monkeypatch.setattr(mcp_router, "_authorize", lambda name, ctx: None)
    res = fn(project_id="pm", kind="characters", name="尹骁", field="右臂")
    if asyncio.iscoroutine(res):
        res = asyncio.run(res)
    assert [r["chapter"] for r in res["rows"]] == [3, 8]
    bad = fn(project_id="nope")
    if asyncio.iscoroutine(bad):
        bad = asyncio.run(bad)
    assert "error" in bad


def test_internal_novel_roles_have_ledger_query():
    cfg = yaml.safe_load((ROOT / "agent_configs" / "novel_chapter.yaml").read_text(encoding="utf-8"))
    for role in ("novel_outliner", "novel_writer", "novel_chapter_reviewer", "novel_finalizer"):
        assert "ledger_query" in (cfg[role].get("tools") or []), role


def test_templates_and_docs_point_at_ledger_query():
    for t in ("novel_outline.md", "novel_draft.md", "novel_draft_review.md", "novel_finalize.md"):
        text = (ROOT / "templates" / t).read_text(encoding="utf-8")
        assert "ledger_query" in text, t
    doc = (ROOT / "docs" / "novel-ledger.md").read_text(encoding="utf-8")
    assert "novel_ledger_query" in doc and "/novel/ledger" in doc
