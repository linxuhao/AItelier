"""The implementation gate must compare on the name the WORKSPACE uses.

`loop_items_implemented` decides whether every planned task has `t_impl`
output, by listing `t_impl/` and checking each manifest id is there. But a
loop item's folder is `_sanitize_item(id)`, which rewrites anything outside
[A-Za-z0-9._-] and appends a hash when it does — `build the login page` lives
in `build_the_login_page-b595fd09`, and any CJK id in `item-<hash>`.

So the raw comparison reports a fully implemented task as missing, from a GATE
whose text tells the reviewer they "reviewed the repository, not the card".
Every manifest in the live workspaces happens to use ASCII snake_case, so this
never fired; it is one PM wording change away.
"""

import json

import pytest

from aitelier.tools.loop_items_implemented.impl import loop_items_implemented
from skillflow.workspace import _sanitize_item


def _workspace(tmp_path, ids):
    """A graph dir with a manifest of `ids` and a t_impl/ folder for each,
    named exactly as the workspace would name it."""
    graph = tmp_path / "dpe_game"
    (graph / "3").mkdir(parents=True)
    (graph / "3" / "tasks_manifest.json").write_text(
        json.dumps({"execution_order": [ids]}), encoding="utf-8")
    impl = graph / "t_impl"
    impl.mkdir()
    for i in ids:
        (impl / _sanitize_item(i)).mkdir()
    return graph


@pytest.mark.parametrize("ids", [
    ["build the login page", "wire up auth"],      # spaces -> hashed folder
    ["任务一", "任务二"],                            # CJK -> item-<hash>
    ["api/auth", "api/users"],                     # slash -> hashed folder
])
def test_implemented_tasks_are_not_reported_missing(tmp_path, ids):
    graph = _workspace(tmp_path, ids)

    out = loop_items_implemented(out_dir=str(graph / "t_impl_review"),
                                 config_name="dpe_game")

    assert out["missing"] == [], (
        f"a fully implemented task was reported missing — the gate compared "
        f"raw ids against folder names: {out['summary']}")
    assert out["complete"] is True
    assert out["implemented"] == len(ids)


def test_a_genuinely_missing_task_is_still_caught(tmp_path):
    """The control. Without it the fix could be 'never report anything'."""
    graph = _workspace(tmp_path, ["任务一", "任务二"])
    # Remove one item's output.
    (graph / "t_impl" / _sanitize_item("任务二")).rmdir()

    out = loop_items_implemented(out_dir=str(graph / "t_impl_review"),
                                 config_name="dpe_game")

    assert out["complete"] is False
    assert out["missing"] == ["任务二"], \
        "the gate must name the RAW id, which is what a human re-plans against"


def test_plain_ascii_ids_are_unaffected(tmp_path):
    """The shape every live manifest actually uses — it must keep working."""
    graph = _workspace(tmp_path, ["fix_click_target", "pin_sprite_ink"])

    out = loop_items_implemented(out_dir=str(graph / "t_impl_review"),
                                 config_name="dpe_game")

    assert out["complete"] is True and out["missing"] == []
