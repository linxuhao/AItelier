"""A generated pipeline must be retirable.

Before this, the catalog could only grow. Deleting `gen_<slug>.yaml` produced a
ZOMBIE: `ConfigRegistry.build` enumerates `sf.list_graphs()` — skillflow's own
`skillflow_graphs` table — so the pipeline stayed listed and runnable while every
file-based tool (`config_read`, `config_edit`, `reload_generated_pipeline`) failed
on it. Clearing the slate for a test round needed hand-written SQL.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from core import pipeline_registry as pr
from core.config_registry import ConfigRegistry


@pytest.fixture
def gen_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_GENERATED_CONFIGS_DIR", str(tmp_path))
    return tmp_path


class _FakeSF:
    """Just enough skillflow: a graph table plus the in-process caches."""

    def __init__(self, names):
        import threading
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("CREATE TABLE skillflow_graphs (name TEXT PRIMARY KEY, "
                           "yaml_text TEXT, version INTEGER)")
        for n in names:
            self._conn.execute("INSERT INTO skillflow_graphs VALUES (?,?,1)",
                               (n, json.dumps({"name": n, "steps": []})))
        self._conn.commit()
        self._lock = threading.RLock()
        self._graphs = {n: object() for n in names}
        self._resolvers = {n: object() for n in names}

    def _get_resolver(self, name):
        row = self._conn.execute(
            "SELECT name FROM skillflow_graphs WHERE name = ?", (name,)).fetchone()
        if not row:
            raise KeyError(name)
        return self._resolvers.setdefault(name, object())

    def list_graphs(self):
        return [{"name": r["name"], "version": r["version"], "description": ""}
                for r in self._conn.execute(
                    "SELECT name, version FROM skillflow_graphs ORDER BY name")]


def _write_pipeline(gen_dir, name):
    (gen_dir / f"{name}.yaml").write_text(f"name: {name}\nsteps: []\n", encoding="utf-8")
    (gen_dir / f"{name}.roles.json").write_text("{}", encoding="utf-8")


def test_archive_moves_the_files_and_records_the_name(gen_dir):
    _write_pipeline(gen_dir, "gen_demo")
    sf, reg = _FakeSF(["gen_demo"]), ConfigRegistry()

    out = pr.archive_generated_pipeline(sf, reg, "gen_demo")

    assert out["archived"] is True and out["purged"] is False
    assert sorted(out["moved"]) == ["gen_demo.roles.json", "gen_demo.yaml"]
    assert not (gen_dir / "gen_demo.yaml").exists()
    assert (pr.archived_dir() / "gen_demo.yaml").exists()
    assert pr.archived_names() == {"gen_demo"}


def test_the_graph_row_survives_a_plain_archive(gen_dir):
    """Existing runs resolve their graph through that row — keep it."""
    _write_pipeline(gen_dir, "gen_demo")
    sf = _FakeSF(["gen_demo"])
    pr.archive_generated_pipeline(sf, ConfigRegistry(), "gen_demo")
    assert [g["name"] for g in sf.list_graphs()] == ["gen_demo"]


def test_purge_deletes_the_graph_row(gen_dir):
    _write_pipeline(gen_dir, "gen_demo")
    sf = _FakeSF(["gen_demo", "dpe_default_v2"])
    out = pr.archive_generated_pipeline(sf, ConfigRegistry(), "gen_demo", purge=True)
    assert out["purged"] is True
    assert [g["name"] for g in sf.list_graphs()] == ["dpe_default_v2"]


def test_an_archived_pipeline_stays_out_of_a_fresh_registry(gen_dir):
    """The zombie test: the graph row is still there, the catalog must not show it."""
    _write_pipeline(gen_dir, "gen_demo")
    sf = _FakeSF(["gen_demo", "dpe_default_v2"])
    pr.archive_generated_pipeline(sf, ConfigRegistry(), "gen_demo")

    rebuilt = ConfigRegistry.build(sf)          # what a restart does
    assert "gen_demo" not in rebuilt.names()


def test_archiving_drops_it_from_the_live_process_too(gen_dir):
    """Otherwise it stays runnable until the next restart."""
    _write_pipeline(gen_dir, "gen_demo")
    sf, reg = _FakeSF(["gen_demo"]), ConfigRegistry()
    reg._manifests["gen_demo"] = object()

    pr.archive_generated_pipeline(sf, reg, "gen_demo")

    assert "gen_demo" not in reg.names()
    assert "gen_demo" not in sf._graphs
    assert "gen_demo" not in sf._resolvers


def test_boot_scan_skips_an_archived_name_even_if_the_file_is_restored(gen_dir,
                                                                      monkeypatch):
    _write_pipeline(gen_dir, "gen_demo")
    sf, reg = _FakeSF(["gen_demo"]), ConfigRegistry()
    pr.archive_generated_pipeline(sf, reg, "gen_demo")
    _write_pipeline(gen_dir, "gen_demo")        # someone copies the YAML back

    registered = []
    monkeypatch.setattr(pr, "_register_text",
                        lambda *a, **k: registered.append(a[2]))
    assert pr.load_generated_configs(sf, reg) == []
    assert registered == []


def test_a_built_in_config_cannot_be_archived(gen_dir):
    out = pr.archive_generated_pipeline(_FakeSF(["dpe_default_v2"]), ConfigRegistry(),
                                        "dpe_default_v2")
    assert "error" in out and "not a generated pipeline" in out["error"]


def test_archiving_is_idempotent(gen_dir):
    _write_pipeline(gen_dir, "gen_demo")
    sf, reg = _FakeSF(["gen_demo"]), ConfigRegistry()
    pr.archive_generated_pipeline(sf, reg, "gen_demo")
    out = pr.archive_generated_pipeline(sf, reg, "gen_demo")
    assert out["archived"] is True and out["moved"] == []
    assert pr.archived_names() == {"gen_demo"}


def test_a_corrupt_exclusion_list_does_not_hide_everything(gen_dir):
    """Fail open: an unreadable list must not silently empty the catalog."""
    pr.archived_dir().joinpath("archived.json").write_text("{not json", encoding="utf-8")
    assert pr.archived_names() == set()
    assert "gen_demo" in ConfigRegistry.build(_FakeSF(["gen_demo"])).names()


class TestReRegisteringLiftsTheTombstone:
    """A config written back into the generated dir must not stay archived.

    Found live: three pipelines that had completed, registered and been used in a
    session were absent from `/api/configs` after a restart, with their YAML sitting
    untouched in `~/.AItelier/configs/`. They had been archived at the start of that
    session's clean-slate setup and re-generated afterwards — `register_forge_pipeline`
    persisted + live-registered them but never cleared the archive entry, so the boot
    scan and `ConfigRegistry.build` both skipped the name. Works until you restart,
    then silently gone.
    """

    def test_persisting_clears_the_archive_entry(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AITELIER_HOME", str(tmp_path))
        import json
        from core import pipeline_registry as pr
        pr._exclusion_file().write_text(json.dumps(["gen_x", "gen_other"]),
                                        encoding="utf-8")
        assert pr._unarchive("gen_x") is True
        assert pr.archived_names() == {"gen_other"}

    def test_it_is_a_no_op_for_a_name_that_was_never_archived(self, tmp_path,
                                                              monkeypatch):
        monkeypatch.setenv("AITELIER_HOME", str(tmp_path))
        from core import pipeline_registry as pr
        assert pr._unarchive("gen_never") is False

    def test_stale_archived_copies_are_removed_so_the_dir_stops_lying(self, tmp_path,
                                                                      monkeypatch):
        monkeypatch.setenv("AITELIER_HOME", str(tmp_path))
        import json
        from core import pipeline_registry as pr
        pr._exclusion_file().write_text(json.dumps(["gen_x"]), encoding="utf-8")
        stale = pr.archived_dir() / "gen_x.yaml"
        stale.write_text("name: gen_x\n", encoding="utf-8")
        assert pr._unarchive("gen_x") is True
        assert not stale.exists()

    def test_the_boot_scan_then_picks_the_config_up(self, tmp_path, monkeypatch):
        """The end of the chain: skip-list empty ⇒ load_generated_configs sees it."""
        monkeypatch.setenv("AITELIER_HOME", str(tmp_path))
        import json
        from core import pipeline_registry as pr
        pr._exclusion_file().write_text(json.dumps(["gen_x"]), encoding="utf-8")
        assert "gen_x" in pr.archived_names()
        pr._unarchive("gen_x")
        assert "gen_x" not in pr.archived_names()
