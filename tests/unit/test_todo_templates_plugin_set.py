"""The plugin's default template set carries every generic template director.md names.

Overfit review 2026-11-24 (重-7): director.md tells every director to use the `find_verify` batch
template, but the plugin alone shipped only `change_files` and `cost_audit`, so a standalone
director's review fan-out was refused as `unknown_template`.

* The generic templates live in the plugin (integrations/dsh-director/todo/todo_templates); the
  repository's own set (todo_templates/) links to them, so both sets carry one source and the
  repository's bytes (and template sha256s) are what they were.
* `find_verify` is the one exception: the repository's copy speaks of this deployment's attempts
  and test rules, so the plugin carries its own. The two differ only in that wording.
* The package alone, with no host hooks and no template setting, lists all of them without error.
"""
from pathlib import Path

import yaml

from tests.unit.test_dsh_todo_package import _GIT_ENV, REPO_ROOT, TODO_ROOT, standalone

PLUGIN_TEMPLATES = TODO_ROOT / "todo_templates"
REPO_TEMPLATES = REPO_ROOT / "todo_templates"
LINKED = ["code_change", "cost_audit", "docs_change", "investigate", "test_fix"]


def test_the_repository_set_links_the_generic_templates_to_the_plugin():
    for name in LINKED:
        path = REPO_TEMPLATES / f"{name}.yaml"
        assert path.is_symlink() and path.resolve() == (PLUGIN_TEMPLATES / f"{name}.yaml").resolve(), name


def test_the_plugin_find_verify_differs_from_the_repository_one_only_in_wording():
    plugin = yaml.safe_load((PLUGIN_TEMPLATES / "find_verify.yaml").read_text(encoding="utf-8"))
    repo = yaml.safe_load((REPO_TEMPLATES / "find_verify.yaml").read_text(encoding="utf-8"))
    assert not (REPO_TEMPLATES / "find_verify.yaml").is_symlink()

    def shape(doc):
        return {**doc, "description": None, "stages": [{**stage, "prompt": None} for stage in doc["stages"]]}

    assert shape(plugin) == shape(repo)
    text = (PLUGIN_TEMPLATES / "find_verify.yaml").read_text(encoding="utf-8")
    assert "aitelier" not in text.lower()


def test_the_package_alone_lists_every_template_director_md_names(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), **_GIT_ENV}
    code, listed = standalone("templates", cwd=tmp_path, env=env)
    assert code == 0, listed
    assert Path(listed["directory"]) == PLUGIN_TEMPLATES
    names = {t["name"]: t for t in listed["templates"]}
    assert set(names) == {"change_files", *LINKED, "find_verify"}
    assert [t for t in names.values() if "error" in t] == []
    director = (TODO_ROOT.parent / "director.md").read_text(encoding="utf-8")
    for named in ("find_verify", "cost_audit"):
        assert named in director and named in names
