"""A generated pipeline config is parsed once per BUILD, not once per reader.

Measured 2026-11-18 in the live container over the deployment's 62 saved
``gen_*.yaml`` files: the capability validator looped the whole directory once
per host capability (3 registered), the output-target migration read it in three
more passes and the release-gate migration in a fourth — 434 graph parses (186
from the capability validator alone) and 55 sidecar parses, 1.6 s of the 3.3 s
first ``get_skillflow()``.

These tests witness the PARSE COUNT, never a wall clock: every ``gen_*.yaml`` and
every ``.roles.json`` sidecar is parsed once for the whole build however many
capabilities or readers ask for it, and the cache revalidates on directory
IDENTITY — ``(name, mtime_ns, size)`` — so a config the forge or the butler writes
while the server runs is seen by the very next reader.
"""

import collections
import hashlib
import json
import os
from pathlib import Path

import pytest
import yaml

from core import capability_registry as caps
from core import output_migration
from core.output_migration import (
    GeneratedConfigs, generated_configs, migrate_generated_outputs)
from core.release_gate_migration import migrate_generated_release_gates


#: Deliberately a document both migrations leave alone: no ``repo_apply``
#: lifecycle, no ``output.target: code``, no DPE release topology and a role
#: prompt with none of the strings the prompt migration rewrites. A build that
#: rewrites a file changes the directory identity, which would make a later
#: reader parse again — correct, but it would blur what these tests measure.
CONFIG_TEMPLATE = """\
name: {name}
description: "{label}"
capabilities:
  - stateful
  - tool_creation
steps:
  - id: plan
    step_type: agent
    agent_config: {name}_planner
    template: {name}_plan.md
    capability: stateful
    transitions:
      - to: build
  - id: build
    step_type: agent
    agent_config: {name}_builder
    template: {name}_build.md
    transitions:
      - to: done
"""

ROLES_TEMPLATE = {
    "{name}_planner": {"system_prompt": "Plan the work. Read the brief, then decide.",
                       "tools": ["read", "create"], "model": "flash"},
    "{name}_builder": {"system_prompt": "Build the work in the worktree.",
                       "tools": ["read", "apply_patch"], "model": "flash"},
}


class _FakeGraph:
    def __init__(self, capabilities):
        self.capabilities = list(capabilities)


class _FakeSF:
    """Just what ``define`` touches.

    It deliberately has no ``_lock``/``_conn``/``_resolvers``/``agent_registry``,
    so ``define`` publishes directly instead of opening a registration
    transaction — the release-graph validation under test is the same either way.
    """

    def __init__(self, graphs=None):
        self._graphs = graphs or {}
        self._capabilities = {}

    def capabilities(self):
        return {name: {"tools": list(cap.get("tools") or ()),
                       "briefing": cap.get("briefing", ""),
                       "owner": cap.get("owner", "host")}
                for name, cap in self._capabilities.items()}

    def graph_capabilities(self, graph_name):
        return list(getattr(self._graphs.get(graph_name), "capabilities", []) or [])

    def register_capability(self, name, *, tools=(), context_provider=None,
                            briefing="", owner="host"):
        self._capabilities[name] = {"tools": list(tools), "briefing": briefing,
                                    "owner": owner,
                                    "context_provider": context_provider}


def _write_config(directory: Path, name: str, *, roles: bool = False,
                  label: str = "") -> Path:
    path = directory / f"{name}.yaml"
    path.write_text(CONFIG_TEMPLATE.format(name=name, label=label or f"{name} graph"),
                    encoding="utf-8")
    if roles:
        (directory / f"{name}.roles.json").write_text(
            json.dumps({role.format(name=name): dict(config)
                        for role, config in ROLES_TEMPLATE.items()},
                       ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """Isolated data root, isolated configs directory, and a COLD parse cache
    (it is module-level, so a pytest session would otherwise start warm)."""
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(output_migration, "_GENERATED_CONFIGS_CACHE", {})
    return tmp_path


@pytest.fixture
def configs_dir(home, monkeypatch):
    directory = home / "configs"
    directory.mkdir()
    monkeypatch.setenv("AITELIER_GENERATED_CONFIGS_DIR", str(directory))
    return directory


def _content_key(value):
    """The parse-count key for one `yaml.safe_load`/`json.loads` argument."""
    if isinstance(value, str):
        value = value.encode("utf-8")
    if isinstance(value, (bytes, bytearray)):
        return hashlib.sha256(bytes(value)).hexdigest()
    return None


@pytest.fixture
def parses(monkeypatch):
    """How many times each generated config / sidecar was PARSED, by file name.

    Counted on `yaml.safe_load`/`json.loads` themselves and attributed by content,
    so the count cannot see WHICH reader asked: a reader that bypassed the shared
    snapshot with its own `yaml.safe_load` is counted here exactly like the ones
    that went through the cache.
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


def _parses_by_name(configs_dir: Path, counts) -> dict[str, int]:
    """Translate content-keyed parse counts into file names."""
    by_name = {}
    for path in sorted(configs_dir.glob("gen_*")):
        key = _content_key(path.read_bytes())
        if key in counts:
            by_name[path.name] = counts[key]
    return by_name


def _read_everything(configs_dir: Path, sf=None) -> _FakeSF:
    """One build's config readers, in the order the boot path runs them: the
    capability validator once per host capability, then both migrations."""
    sf = sf if sf is not None else _FakeSF()
    for name in ("stateful", "tool_creation", "game_assets"):
        assert caps.define(sf, name, tools=[], briefing="",
                           owner="host").get("ok") is True
    assert migrate_generated_outputs(configs_dir) == []
    assert migrate_generated_release_gates(configs_dir) == []
    return sf


def test_each_config_is_parsed_once_per_build_not_once_per_capability(
        configs_dir, parses):
    """The defect in one assertion: three capabilities validating the same five
    configs must not parse them fifteen times."""
    names = [f"gen_pipeline_{suffix}"
             for suffix in ("alpha", "beta", "gamma", "delta", "epsilon")]
    for index, name in enumerate(names):
        _write_config(configs_dir, name, roles=index % 2 == 0)

    _read_everything(configs_dir)

    expected = {f"{name}.yaml": 1 for name in names}
    expected.update({f"{names[i]}.roles.json": 1 for i in (0, 2, 4)})
    assert _parses_by_name(configs_dir, parses) == expected


def test_the_migration_readers_share_the_capability_pass(configs_dir, parses):
    """`_code_roles`, `_readonly_verifier_roles`, the output-target migration, the
    role-prompt migration and the release-gate migration all ask for the same
    directory; between them they add no parse to the one the validator did."""
    for name in ("gen_pipeline_alpha", "gen_pipeline_beta"):
        _write_config(configs_dir, name, roles=True)

    configs = generated_configs(configs_dir)
    # The capability validator's own pass, then each migration reader in turn.
    for _ in range(3):
        assert caps._release_graph_update_error(
            _FakeSF(), "stateful", {"tools": []}) == ""
    assert migrate_generated_outputs(configs_dir) == []
    assert migrate_generated_release_gates(configs_dir) == []
    # The snapshot object is reused, so nothing was re-scanned into a new one.
    assert generated_configs(configs_dir) is configs

    counted = _parses_by_name(configs_dir, parses)
    assert set(counted) == {path.name for path in configs_dir.glob("gen_*")}
    assert set(counted.values()) == {1}


def test_an_unchanged_directory_costs_no_parse_on_the_next_build(
        configs_dir, parses):
    """A second read of an UNCHANGED directory costs one glob+stat pass and zero
    parses — the cache is keyed on what is on disk, not on how often it was asked."""
    for name in ("gen_pipeline_alpha", "gen_pipeline_beta"):
        _write_config(configs_dir, name, roles=True)
    _read_everything(configs_dir)
    first_build = _parses_by_name(configs_dir, parses)
    assert set(first_build.values()) == {1}

    _read_everything(configs_dir)

    assert _parses_by_name(configs_dir, parses) == first_build


def test_touching_one_config_revalidates_the_whole_directory(configs_dir, parses):
    """Identity is ``(name, mtime_ns, size)``, never a call count: a forge rewrite
    or a plain `touch` while the server runs is seen by the next reader."""
    for name in ("gen_pipeline_alpha", "gen_pipeline_beta"):
        _write_config(configs_dir, name)
    _read_everything(configs_dir)
    assert set(_parses_by_name(configs_dir, parses).values()) == {1}

    beta = configs_dir / "gen_pipeline_beta.yaml"
    stat = beta.stat()
    os.utime(beta, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    _read_everything(configs_dir)

    assert set(_parses_by_name(configs_dir, parses).values()) == {2}


def test_a_rewritten_or_new_config_is_seen_by_the_next_reader(configs_dir):
    """The forge and the butler write `gen_*.yaml` and live-register it in the
    same process; the next reader must read the file, not a start-of-process view."""
    alpha = _write_config(configs_dir, "gen_pipeline_alpha")
    assert generated_configs(configs_dir).document(alpha)["description"] == \
        "gen_pipeline_alpha graph"

    _write_config(configs_dir, "gen_pipeline_alpha", label="alpha rewritten")
    snapshot = generated_configs(configs_dir)
    assert snapshot.document(alpha)["description"] == "alpha rewritten"
    assert [path.name for path in snapshot.paths] == ["gen_pipeline_alpha.yaml"]

    beta = _write_config(configs_dir, "gen_pipeline_beta", roles=True)
    snapshot = generated_configs(configs_dir)
    assert [path.name for path in snapshot.paths] == [
        "gen_pipeline_alpha.yaml", "gen_pipeline_beta.yaml"]
    assert snapshot.has_roles(beta) is True
    assert snapshot.roles(beta) == json.loads(
        (configs_dir / "gen_pipeline_beta.roles.json").read_bytes())


def test_the_cached_documents_are_field_identical_to_a_direct_parse(configs_dir):
    """Nothing is lost by sharing the parse: every cached document and sidecar is
    deep-equal to the bytes on disk, and the same object serves every reader."""
    path = _write_config(configs_dir, "gen_pipeline_alpha", roles=True)
    snapshot = generated_configs(configs_dir)

    assert snapshot.document(path) == yaml.safe_load(path.read_bytes())
    assert snapshot.roles(path) == json.loads(
        (configs_dir / "gen_pipeline_alpha.roles.json").read_bytes())
    # Shared, not re-parsed: the same object every time, for every reader.
    assert snapshot.document(path) is generated_configs(configs_dir).document(path)
    assert snapshot.roles(path) is generated_configs(configs_dir).roles(path)
    # An offer list is one of the fields that must survive untouched.
    assert snapshot.document(path)["capabilities"] == ["stateful", "tool_creation"]


def test_a_migration_writer_gets_a_private_copy(configs_dir):
    """`migrate_document`/`migrate_release_document` rewrite the dict they are
    handed. The writers ask for `mutable=True`, so a half-finished migration can
    never leak into the parse the next reader sees."""
    path = _write_config(configs_dir, "gen_pipeline_alpha", roles=True)
    snapshot = generated_configs(configs_dir)
    before = yaml.safe_load(path.read_bytes())

    document = snapshot.document(path, mutable=True)
    document["steps"][0]["tool_params"] = {"injected": True}
    document["capabilities"].append("game_assets")
    roles = snapshot.roles(path, mutable=True)
    roles["gen_pipeline_alpha_planner"]["system_prompt"] = "rewritten"

    assert snapshot.document(path) == before
    assert snapshot.roles(path) == json.loads(
        (configs_dir / "gen_pipeline_alpha.roles.json").read_bytes())


def test_the_parse_cache_does_not_grow_with_every_directory(tmp_path, parses):
    """The deployment keeps ONE configs directory; a test session (or a forge
    writing to a throwaway dir) must not make the cache grow without bound — and
    evicting an old directory must not cost the current one its parse."""
    directories = []
    for index in range(output_migration.MAX_CACHED_CONFIG_DIRS + 2):
        directory = tmp_path / f"configs_{index}"
        directory.mkdir()
        _write_config(directory, f"gen_pipeline_{index}")
        directories.append(directory)
        generated_configs(directory)

    assert len(output_migration._GENERATED_CONFIGS_CACHE) == \
        output_migration.MAX_CACHED_CONFIG_DIRS

    last = directories[-1]
    config = last / f"gen_pipeline_{len(directories) - 1}.yaml"
    assert generated_configs(last).paths == (config,)
    assert generated_configs(last).document(config)["name"] == \
        f"gen_pipeline_{len(directories) - 1}"
    assert set(_parses_by_name(last, parses).values()) == {1}


def test_both_migrations_hand_their_writer_a_private_copy(configs_dir, monkeypatch):
    """A writer that mutated the shared parse would change what the NEXT reader of
    the same build validates (and a failed write would keep it poisoned), so both
    migration writers must hand `migrate_document`/`migrate_release_document`
    something that is not the cached object."""
    for name in ("gen_pipeline_alpha", "gen_pipeline_beta"):
        _write_config(configs_dir, name)
    handed = []

    def record(document, **_kwargs):
        handed.append(document)
        return []

    monkeypatch.setattr(output_migration, "migrate_document", record)
    monkeypatch.setattr("core.release_gate_migration.migrate_release_document",
                        record)

    migrate_generated_outputs(configs_dir)
    migrate_generated_release_gates(configs_dir)

    snapshot = generated_configs(configs_dir)
    shared = {id(snapshot.document(path)) for path in snapshot.paths}
    assert len(handed) == 4                 # both writers, both configs
    assert not shared & {id(document) for document in handed}


def test_capability_offers_and_registrations_survive_the_shared_pass(
        configs_dir, monkeypatch):
    """Before/after in one test: the shared parse and the old per-reader parse
    reach the same registrations, and every graph keeps the capabilities it
    offers on disk."""
    names = [f"gen_pipeline_{suffix}" for suffix in ("alpha", "beta", "gamma")]
    for name in names:
        _write_config(configs_dir, name, roles=True)
    offered = {name: list((yaml.safe_load(
        (configs_dir / f"{name}.yaml").read_bytes()) or {}).get("capabilities") or ())
        for name in names}
    assert all(offered.values())          # never a vacuous comparison

    shared = _read_everything(configs_dir, _FakeSF(
        {name: _FakeGraph(offer) for name, offer in offered.items()}))

    # The old behaviour, as a control: a cold parse per reader.
    def cold(config_dir):
        config_dir = Path(config_dir)
        return GeneratedConfigs(
            config_dir, ("cold",),
            tuple(sorted(config_dir.glob("gen_*.yaml"))),
            tuple(path.name for path in sorted(config_dir.glob("gen_*.roles.json"))))

    monkeypatch.setattr(output_migration, "generated_configs", cold)
    monkeypatch.setattr("core.release_gate_migration.generated_configs", cold)
    per_reader = _read_everything(configs_dir, _FakeSF(
        {name: _FakeGraph(offer) for name, offer in offered.items()}))

    assert shared._capabilities == per_reader._capabilities
    for name in ("stateful", "tool_creation", "game_assets"):
        assert caps.offering_configs(shared, name) == \
            caps.offering_configs(per_reader, name) == \
            sorted(config for config, offers in offered.items() if name in offers)
