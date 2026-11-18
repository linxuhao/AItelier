"""The boot scan parses each generated config ONCE, not once per pass.

``load_generated_configs`` walks the ``gen_*.yaml`` directory three times: the
release-gate ownership pre-check, the two migrations, and the registration loop.
Each pass used to parse the same bytes for itself — and the registration loop
parsed the file as text and validated the text, which parses it again — so one
boot over a ``gen_*`` directory cost several parses per config. The fix routes
every pass through ``core.output_migration.generated_configs``, the build's single
identity-keyed parse.

These tests count PARSES, never a wall clock: each ``yaml.safe_load`` and
``json.loads`` argument is hashed, so a reader that bypassed the shared snapshot
with its own parse is counted here exactly like one that went through the cache.
The two executed invalid-file cases pin the other half of the contract — a file
the reader cannot parse, or a sidecar that is not a JSON object, is SKIPPED and
every healthy config in the same directory still registers.
"""

import collections
import hashlib
import json
import logging
from pathlib import Path

import pytest
import yaml
from skillflow import SkillFlow

from core import output_migration
from core import pipeline_registry as pr
from core.config_registry import ConfigRegistry


#: A generated-style config with already-namespaced roles, no git/repo shape and
#: no DPE release topology — so both boot migrations leave the bytes alone. That
#: matters here: a rewritten file is a DIFFERENT content, and re-parsing it is
#: correct but would blur the per-file count these tests measure.
CONFIG_TEMPLATE = """\
name: {name}
description: "{label}"
begin: plan
steps:
  - id: plan
    step_type: agent
    agent_config: {name}__planner
    transitions:
      - to: build
  - id: build
    step_type: agent
    agent_config: {name}__builder
    transitions:
      - to: null
"""

ROLES_TEMPLATE = {
    "{name}__planner": {"model": "host", "tools": ["read_file", "write"],
                        "system_prompt": "Plan the work from the brief."},
    "{name}__builder": {"model": "host", "tools": ["read_file", "write"],
                        "system_prompt": "Build the work in the staged output."},
}


@pytest.fixture(autouse=True)
def configs_dir(tmp_path, monkeypatch):
    """Isolated data root, isolated configs directory, and a COLD parse cache
    (it is module-level, so a pytest session would otherwise start warm)."""
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(output_migration, "_GENERATED_CONFIGS_CACHE", {})
    directory = tmp_path / "configs"
    directory.mkdir()
    monkeypatch.setenv("AITELIER_GENERATED_CONFIGS_DIR", str(directory))
    return directory


@pytest.fixture
def sf():
    return SkillFlow(":memory:")


def _write_config(directory: Path, name: str, *, roles: bool = True) -> Path:
    path = directory / f"{name}.yaml"
    path.write_text(CONFIG_TEMPLATE.format(name=name, label=f"{name} graph"),
                    encoding="utf-8")
    if roles:
        (directory / f"{name}.roles.json").write_text(
            json.dumps({role.format(name=name): dict(config)
                        for role, config in ROLES_TEMPLATE.items()},
                       ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
    return path


def _content_key(value):
    """The parse-count key for one `yaml.safe_load`/`json.loads` argument."""
    if isinstance(value, str):
        value = value.encode("utf-8")
    if isinstance(value, (bytes, bytearray)):
        return hashlib.sha256(bytes(value)).hexdigest()
    return None


@pytest.fixture
def parses(monkeypatch):
    """How many times the bytes of each file were parsed, keyed by content hash.

    Counted on `yaml.safe_load`/`json.loads` themselves, so the count cannot see
    WHICH pass asked: a pass that bypassed the shared snapshot with its own parse
    is counted exactly like one that went through the cache.
    """
    counts: collections.Counter = collections.Counter()
    real_yaml = yaml.safe_load
    real_json = json.loads

    def counting_yaml(stream, *args, **kwargs):
        key = _content_key(stream)
        if key is not None:
            counts[key] += 1
        return real_yaml(stream, *args, **kwargs)

    def counting_json(raw, *args, **kwargs):
        key = _content_key(raw)
        if key is not None:
            counts[key] += 1
        return real_json(raw, *args, **kwargs)

    monkeypatch.setattr(yaml, "safe_load", counting_yaml)
    monkeypatch.setattr(json, "loads", counting_json)
    return counts


def _parses_by_name(directory: Path, counts) -> dict[str, int]:
    """Translate content-keyed parse counts into file names."""
    by_name = {}
    for path in sorted(directory.glob("gen_*")):
        key = _content_key(path.read_bytes())
        if key in counts:
            by_name[path.name] = counts[key]
    return by_name


def test_one_boot_pass_parses_each_config_once(configs_dir, parses, sf):
    """The defect in one assertion: five configs and three sidecars over one boot
    scan must not cost a parse per pass."""
    names = [f"gen_pipeline_{suffix}"
             for suffix in ("alpha", "beta", "gamma", "delta", "epsilon")]
    for index, name in enumerate(names):
        _write_config(configs_dir, name, roles=index % 2 == 0)
    with_roles = {f"{names[i]}.roles.json" for i in (0, 2, 4)}
    before = {path.name: path.read_bytes() for path in configs_dir.glob("gen_*")}

    loaded = pr.load_generated_configs(sf, ConfigRegistry())

    assert sorted(loaded) == sorted(names)
    # Neither migration rewrote a file, so the count below is one content per file.
    assert {path.name: path.read_bytes()
            for path in configs_dir.glob("gen_*")} == before

    counted = _parses_by_name(configs_dir, parses)
    assert set(counted) == {path.name for path in configs_dir.glob("gen_*")}
    # AT MOST once per config and per sidecar — the whole boot scan shares one parse.
    assert {name: count for name, count in counted.items()
            if name not in with_roles} == {
                f"{name}.yaml": 1 for name in names}
    assert {name: count for name, count in counted.items()
            if name in with_roles} == {name: 1 for name in with_roles}


def test_a_second_boot_pass_on_unchanged_bytes_parses_nothing(
        configs_dir, parses, sf):
    """A restart's second scan of the same directory has nothing new to read: the
    cache is keyed on directory identity, not on a call count."""
    _write_config(configs_dir, "gen_pipeline_alpha")
    _write_config(configs_dir, "gen_pipeline_beta", roles=False)
    assert pr.load_generated_configs(sf, ConfigRegistry()) == [
        "gen_pipeline_alpha", "gen_pipeline_beta"]
    first = _parses_by_name(configs_dir, parses)
    assert set(first.values()) == {1}

    assert pr.load_generated_configs(sf, ConfigRegistry()) == [
        "gen_pipeline_alpha", "gen_pipeline_beta"]

    assert _parses_by_name(configs_dir, parses) == first


def test_a_config_that_is_not_yaml_is_skipped_and_its_siblings_still_register(
        configs_dir, caplog, sf):
    """Executed invalid case 1: bytes that are not valid YAML. The boot scan logs
    it and registers the healthy configs in the SAME directory."""
    _write_config(configs_dir, "gen_pipeline_alpha")
    broken = configs_dir / "gen_pipeline_broken.yaml"
    broken.write_bytes(b"name: [unclosed\nsteps: : :\n")
    _write_config(configs_dir, "gen_pipeline_beta")

    with caplog.at_level(logging.WARNING, logger=pr.__name__):
        loaded = pr.load_generated_configs(sf, ConfigRegistry())

    assert loaded == ["gen_pipeline_alpha", "gen_pipeline_beta"]
    assert "gen_pipeline_broken.yaml" in caplog.text
    assert sf._graphs.get("gen_pipeline_alpha") is not None
    assert sf._graphs.get("gen_pipeline_beta") is not None
    assert "gen_pipeline_broken" not in sf._graphs
    # Its bytes were never rewritten behind the user's back.
    assert broken.read_bytes() == b"name: [unclosed\nsteps: : :\n"


def test_a_sidecar_that_is_not_a_json_object_is_skipped_and_siblings_still_register(
        configs_dir, caplog, sf):
    """Executed invalid case 2: a ``.roles.json`` that is malformed JSON. The
    config it belongs to is skipped; the healthy configs still register."""
    _write_config(configs_dir, "gen_pipeline_alpha")
    _write_config(configs_dir, "gen_pipeline_broken")
    broken_roles = configs_dir / "gen_pipeline_broken.roles.json"
    broken_roles.write_text('{"gen_pipeline_broken__planner": ', encoding="utf-8")
    _write_config(configs_dir, "gen_pipeline_beta", roles=False)

    with caplog.at_level(logging.WARNING, logger=pr.__name__):
        loaded = pr.load_generated_configs(sf, ConfigRegistry())

    assert loaded == ["gen_pipeline_alpha", "gen_pipeline_beta"]
    assert "gen_pipeline_broken.roles.json" in caplog.text
    assert sf._graphs.get("gen_pipeline_alpha") is not None
    assert sf._graphs.get("gen_pipeline_beta") is not None
    assert "gen_pipeline_broken" not in sf._graphs
    assert broken_roles.read_text(encoding="utf-8") == \
        '{"gen_pipeline_broken__planner": '


def test_a_sidecar_that_is_valid_json_but_not_an_object_is_skipped(
        configs_dir, caplog, sf):
    """A JSON list is parseable and still unusable as a role table — the skip must
    not become a crash or a registration with the generic fallback prompt."""
    _write_config(configs_dir, "gen_pipeline_broken", roles=False)
    (configs_dir / "gen_pipeline_broken.roles.json").write_text(
        '["gen_pipeline_broken__planner"]', encoding="utf-8")
    _write_config(configs_dir, "gen_pipeline_alpha")

    with caplog.at_level(logging.WARNING, logger=pr.__name__):
        loaded = pr.load_generated_configs(sf, ConfigRegistry())

    assert loaded == ["gen_pipeline_alpha"]
    assert "gen_pipeline_broken.yaml" in caplog.text
    assert "gen_pipeline_broken" not in sf._graphs
