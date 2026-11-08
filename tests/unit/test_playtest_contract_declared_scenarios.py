"""A scenario list belongs to the scenario FILES, never to the shared fragment.

``_read_split`` (aitelier/tools/godot_playtest/impl.py) assembles the contract
from ``playtest/``: one file per scenario, plus a ``_common.yaml`` carrying what
they share and a ``scenario_order``. It used to pop only ``scenario_order`` and
then assign

    spec["scenarios"] = [found[n] for n in names]

so a ``scenarios:`` list an author wrote in ``playtest/_common.yaml`` was
discarded — the file-derived list was substituted, no error, no note. An author
who declared which scenarios run got a run of a different set. Measured
2026-11-08: ``evidence/playtest-two-reading-20261108/report.json``, row
``contract-common-scenarios``, class ``silent-default``.

The repair refuses the declaration in the same ``errors`` list as the loader's
other refusals (unreadable fragment, ghost ``scenario_order`` name, file with no
``timeline:``, ``name:``/basename mismatch), naming ``scenarios`` and the shared
fragment, and hands back NO contract — so nothing downstream can carry a list
the author did not write.

The two shapes the refusal must not touch:
  * the ordinary split contract — files in ``scenario_order``, unlisted files
    appended sorted, shared keys passed through (today's documented behaviour);
  * the monolithic ``playtest_spec.yaml`` — there ``scenarios:`` IS the
    contract, and its list loads unchanged.
"""

from aitelier.tools.godot_playtest.impl import read_spec


def _write_split(root, common, scenarios):
    """The fixture shape the repository's other play-test tests build."""
    d = root / "playtest"
    d.mkdir()
    import yaml
    (d / "_common.yaml").write_text(yaml.safe_dump(common, sort_keys=False),
                                    encoding="utf-8")
    for sc in scenarios:
        (d / f"{sc['name']}.yaml").write_text(yaml.safe_dump(sc, sort_keys=False),
                                              encoding="utf-8")
    return d


def _scenario(name, at):
    return {"name": name,
            "timeline": [{"at": at, "assert": {"HUD/Score.text": str(at)}}]}


# ── the defect: a declared list is refused, not replaced ───────────────────

def test_a_scenario_list_declared_in_the_shared_fragment_is_refused(tmp_path):
    """The author wrote a run of exactly one scenario (``from-common``) in the
    shared fragment while three scenario FILES sit beside it. Silently running
    the files would report pass/fail over scenarios the author never declared.
    """
    _write_split(
        tmp_path,
        {"scene": "res://main.tscn",
         "scenarios": [{"name": "from-common",
                        "timeline": [{"at": 5, "press": "ghost"}]}]},
        [_scenario("alpha", 1), _scenario("beta", 2), _scenario("gamma", 3)])

    spec, info = read_spec(tmp_path)

    # Refused, in the loader's error list, naming the key AND the file.
    assert info["errors"], "a declared scenario list must never load silently"
    assert any("scenarios" in e and "_common.yaml" in e for e in info["errors"]), \
        info["errors"]

    # And no contract comes back, so the assembled spec cannot carry the
    # file-derived list (alpha/beta/gamma) in place of the author's own
    # (from-common). `source` stays empty: nothing in this repo was read.
    assert spec is None
    assert info["source"] == ""


def test_the_refusal_fires_on_presence_not_on_truthiness(tmp_path):
    """``scenarios: []`` is still a list written in the shared fragment, and it
    would be swapped for the files' list just the same — so it is refused
    rather than read as "no opinion"."""
    _write_split(tmp_path,
                 {"scene": "res://main.tscn", "scenarios": []},
                 [_scenario("alpha", 1)])

    spec, info = read_spec(tmp_path)

    assert spec is None
    assert any("scenarios" in e and "_common.yaml" in e for e in info["errors"]), \
        info["errors"]


def test_the_defect_is_reported_next_to_the_loaders_other_refusals(tmp_path):
    """Order and company matter: the declared list is one more thing that would
    have silently shrunk the contract, so it is reported like the ghost
    ``scenario_order`` name — in the same list, not as a crash and not as a
    note a reviewer has to go looking for."""
    _write_split(
        tmp_path,
        {"scenarios": [{"name": "from-common", "timeline": []}],
         "scenario_order": ["alpha", "ghost"]},
        [_scenario("alpha", 1)])

    spec, info = read_spec(tmp_path)

    assert spec is None
    joined = " | ".join(info["errors"])
    assert "scenarios" in joined and "_common.yaml" in joined
    assert "ghost" in joined and "scenario_order" in joined
    assert info["notes"] == []


# ── the shape that must keep working: the split contract ───────────────────

def test_the_ordinary_split_contract_still_assembles_unchanged(tmp_path):
    """Three scenario files, a shared fragment with NO ``scenarios:`` key:
    ``scenario_order`` fixes the run order, unlisted files are appended sorted,
    and the fragment's shared keys come through untouched. This is today's
    documented behaviour and the repair must not move it."""
    _write_split(
        tmp_path,
        {"scene": "res://main.tscn",
         "actions": ["ui_accept"],
         "surface": {"Player": ["health"]},
         "scenario_order": ["gamma", "alpha"]},
        [_scenario("zeta", 3), _scenario("alpha", 1), _scenario("gamma", 2)])

    spec, info = read_spec(tmp_path)

    assert info["errors"] == []
    assert info["source"] == "playtest/"
    assert [s["name"] for s in spec["scenarios"]] == ["gamma", "alpha", "zeta"]
    assert spec["scene"] == "res://main.tscn"
    assert spec["actions"] == ["ui_accept"]
    assert spec["surface"] == {"Player": ["health"]}
    # `scenario_order` is a loader directive, not part of the contract the
    # sidecar is handed.
    assert "scenario_order" not in spec
    # The scenarios are the files themselves, not a copy rewritten on the way.
    assert spec["scenarios"][0]["timeline"] == _scenario("gamma", 2)["timeline"]


# ── the shape that must keep working: the monolith ─────────────────────────

def test_the_monolithic_contract_still_loads_its_own_scenario_list(tmp_path):
    """``playtest_spec.yaml`` IS the contract: its ``scenarios:`` list is the
    author's list, in the author's order, and this reader acquires no opinion
    about it. No ``playtest/`` directory exists here, so nothing reassembles
    anything."""
    (tmp_path / "playtest_spec.yaml").write_text(
        "scene: res://main.tscn\n"
        "scenarios:\n"
        "  - name: second\n"
        "    timeline: [{at: 20, assert: {HUD/Score.text: '2'}}]\n"
        "  - name: first\n"
        "    timeline: [{at: 10, assert: {HUD/Score.text: '1'}}]\n",
        encoding="utf-8")

    spec, info = read_spec(tmp_path)

    assert info["errors"] == []
    assert info["source"] == "playtest_spec.yaml"
    assert spec is not None
    # Author's order — not sorted, not file-derived, not re-derived at all.
    assert [s["name"] for s in spec["scenarios"]] == ["second", "first"]
    assert spec["scenarios"][0]["timeline"] == [
        {"at": 20, "assert": {"HUD/Score.text": "2"}}]
