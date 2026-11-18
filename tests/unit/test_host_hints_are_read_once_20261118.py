"""The host-hint dict costs one ``configs/*.yaml`` pass per build, not per graph.

Measured 2026-11-18 in the live container: reading the ``x-aitelier:`` blocks cost
178 ms (14 files, 215 817 bytes), and the first ``get_skillflow()`` — 14 130 ms —
paid that read 64 times in one build, 63 of them from ``register_one`` for the
generated ``gen_*`` graphs. ``ConfigRegistry.build`` already read the dict once;
``register_one`` had no dict to reuse and re-read the directory for every graph.

These tests witness the PARSE COUNT, never the wall clock: each file of the host
``configs/`` directory is parsed once per build however many graphs are
registered, and the cache revalidates on file IDENTITY — ``(path, mtime_ns,
size)`` — so a config the forge or the butler adds or edits while the server runs
is seen by the very next build.
"""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from core import config_registry
from core.config_registry import ConfigRegistry


CONFIG_TEMPLATE = """\
name: {name}
description: "{name} graph"
x-aitelier:
  label: "{label}"
  scheduler_owned: {scheduler_owned}
  seed_file: {seed_file}
steps:
  - id: only
    type: agent
    agent_config: writer
    template: x.md
"""


class _FakeResolver:
    def __init__(self, name: str) -> None:
        self.graph = SimpleNamespace(
            name=name, description=f"{name} graph", steps=[])


class _FakeSF:
    """Just the two calls ``ConfigRegistry`` makes: ``list_graphs`` and
    ``_get_resolver``. Registering a graph is not part of the manifest path."""

    def __init__(self, names) -> None:
        self.names = list(names)

    def list_graphs(self):
        return [{"name": name} for name in self.names]

    def _get_resolver(self, name):
        if name not in self.names:
            raise KeyError(name)
        return _FakeResolver(name)


@pytest.fixture(autouse=True)
def cold_cache(monkeypatch):
    """The cache is module-level, so every test here starts from a cold read."""
    monkeypatch.setattr(config_registry, "_HOST_HINT_CACHE", None)


@pytest.fixture
def parses(monkeypatch):
    """How many times each ``configs/*.yaml`` file was parsed (per file NAME)."""
    counts: dict[str, int] = {}
    real = config_registry._load_config_document

    def counting(path):
        name = Path(path).name
        counts[name] = counts.get(name, 0) + 1
        return real(path)

    monkeypatch.setattr(config_registry, "_load_config_document", counting)
    return counts


@pytest.fixture
def host_configs(tmp_path, monkeypatch):
    """A stand-in ``configs/`` directory holding three hinted host graphs."""
    configs = tmp_path / "configs"
    configs.mkdir()
    for index, name in enumerate(("alpha_host", "beta_host", "gamma_host")):
        (configs / f"{name}.yaml").write_text(
            CONFIG_TEMPLATE.format(
                name=name, label=f"{name} label",
                scheduler_owned="true" if index == 0 else "false",
                seed_file=f"{name}_seed.md"),
            encoding="utf-8")
    monkeypatch.setattr(config_registry, "CONFIGS_DIR", configs)
    return configs


def test_each_config_is_parsed_once_per_build_not_once_per_graph(
        host_configs, parses):
    """The defect in one assertion: registering five generated graphs on top of a
    three-file build must not multiply the parses."""
    generated = ["gen_one", "gen_two", "gen_three", "gen_four"]
    sf = _FakeSF(["alpha_host", "beta_host", "gamma_host", *generated])

    registry = ConfigRegistry.build(sf)
    # The boot scan then live-registers every persisted gen_* graph, exactly as
    # `load_generated_configs` does through `_commit_registration`.
    for name in generated:
        assert registry.register_one(sf, name) is not None

    assert parses == {"alpha_host.yaml": 1, "beta_host.yaml": 1,
                      "gamma_host.yaml": 1}


def test_build_and_live_registration_observe_the_same_hint_dict(
        host_configs, parses):
    """`build` and the per-graph `register_one` path share one dict object, so the
    hints a generated graph sees cannot drift from the ones a built graph saw."""
    sf = _FakeSF(["alpha_host", "gen_live"])
    registry = ConfigRegistry.build(sf)
    build_hints = config_registry._read_host_hints()
    parses_after_build = dict(parses)

    assert config_registry._read_host_hints() is build_hints
    registry.register_one(sf, "gen_live")

    assert config_registry._read_host_hints() is build_hints
    assert parses == parses_after_build      # live registration parsed nothing


def test_the_boot_registration_path_reuses_the_build_hint_dict(
        host_configs, parses):
    """`load_generated_configs`/`register_generated_pipeline` register through
    `_commit_registration` → `register_one`; neither re-parses the host configs a
    build already read, and the generated hints still win where they are passed."""
    from core.pipeline_registry import _commit_registration

    sf = _FakeSF(["alpha_host", "gen_committed"])
    registry = ConfigRegistry.build(sf)
    assert set(parses.values()) == {1}

    published = []

    class _BootSF(_FakeSF):
        agent_registry = {}

        def register_agent_config_from_dict(self, role, config):
            self.agent_registry[role] = config

        def register_graph(self, graph):
            published.append(graph)

    boot_sf = _BootSF(sf.names)
    graph = SimpleNamespace(name="gen_committed", description="", steps=[])

    _commit_registration(boot_sf, registry, "gen_committed", graph,
                         {"label": "Committed Pipeline"})

    assert published == [graph]
    assert set(parses.values()) == {1}      # one parse per file for the whole build
    assert registry.get("gen_committed").label == "Committed Pipeline"


def test_an_unchanged_directory_is_not_reparsed_by_the_next_build(
        host_configs, parses):
    sf = _FakeSF(["alpha_host", "beta_host", "gamma_host"])
    ConfigRegistry.build(sf)
    assert set(parses.values()) == {1}

    ConfigRegistry.build(sf)

    assert set(parses.values()) == {1}


def test_touching_one_config_revalidates_the_whole_directory(
        host_configs, parses):
    """A config the forge/butler rewrites while the server runs stays visible:
    identity is (path, mtime_ns, size), never a call count or process start."""
    sf = _FakeSF(["alpha_host", "beta_host", "gamma_host"])
    ConfigRegistry.build(sf)
    assert set(parses.values()) == {1}

    beta = host_configs / "beta_host.yaml"
    stat = beta.stat()
    # Same bytes, new mtime — a rewrite or a `touch`, not a content change.
    os.utime(beta, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    ConfigRegistry.build(sf)

    assert set(parses.values()) == {2}

    beta.write_text(
        CONFIG_TEMPLATE.format(name="beta_host", label="beta rewritten",
                               scheduler_owned="false",
                               seed_file="beta_host_seed.md"),
        encoding="utf-8")
    registry = ConfigRegistry.build(sf)

    assert parses["beta_host.yaml"] == 3
    assert registry.get("beta_host").label == "beta rewritten"


def test_a_config_added_at_runtime_is_seen_by_the_next_registration(
        host_configs, parses):
    """The forge writes `gen_*.yaml` and live-registers it in the same process;
    that registration must read the new file rather than a start-of-process view."""
    sf = _FakeSF(["alpha_host"])
    ConfigRegistry.build(sf)
    assert set(parses.values()) == {1}

    (host_configs / "gen_fresh.yaml").write_text(
        CONFIG_TEMPLATE.format(name="gen_fresh", label="Fresh Forge Pipeline",
                               scheduler_owned="false",
                               seed_file="gen_fresh_seed.md"),
        encoding="utf-8")
    sf.names.append("gen_fresh")

    manifest = ConfigRegistry().register_one(sf, "gen_fresh")

    assert manifest is not None
    assert manifest.label == "Fresh Forge Pipeline"
    assert manifest.scheduler_owned is False
    assert manifest.seed_file == "gen_fresh_seed.md"
    assert parses["gen_fresh.yaml"] == 1


def test_hint_overrides_still_layer_on_top_of_the_declared_block(host_configs):
    """`register_one(host_hints=…)` must keep working for callers that hold a
    dict, and `hint_overrides` must still win over the declared block."""
    sf = _FakeSF(["alpha_host"])
    declared = config_registry._read_host_hints()

    registry = ConfigRegistry()
    manifest = registry.register_one(
        sf, "alpha_host", hint_overrides={"label": "Overridden"},
        host_hints=declared)
    assert manifest.label == "Overridden"
    assert manifest.scheduler_owned is True          # declared, not overridden
    assert manifest.seed_file == "alpha_host_seed.md"

    # No dict passed: the same declared hints come from the cache.
    from_cache = registry.register_one(sf, "alpha_host")
    assert from_cache.label == "alpha_host label"
    assert from_cache.seed_file == "alpha_host_seed.md"


# The declared key → manifest field mapping, so the real configs prove it
# independently of `_make_manifest`'s own code path.
_DECLARED_KEY_FIELDS = {
    "label": "label",
    "has_task_loop": "has_task_loop",
    "scheduler_owned": "scheduler_owned",
    "seed_file": "seed_file",
    "seed_default": "seed_default",
    "output_step": "output_step",
    "input_hint": "input_hint",
    "registers_generated_pipeline": "registers_generated_pipeline",
    "registers_generated_addon": "registers_generated_addon",
    "repo_mode": "repo_mode",
    "preamble_steps": "preamble_steps",
    "labels": "label_overrides",
    "checkpoint_kind": "checkpoint_kind",
    "manual_checkpoints_only": "manual_checkpoints_only",
    "checkpoint_kinds": "checkpoint_kinds",
    "listed": "listed",
}


def test_every_real_config_block_still_reaches_its_manifest():
    """Against the repository's own ``configs/``: no graph that declares an
    ``x-aitelier`` block loses a field now that the dict is cached."""
    real_dir = Path(config_registry.__file__).resolve().parent.parent / "configs"
    declared = {}
    for path in sorted(real_dir.glob("*.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        name = document.get("name")
        block = document.get("x-aitelier")
        if name and isinstance(block, dict) and block:
            declared[name] = block
    # Guard against a vacuous pass if the directory ever stops carrying blocks.
    assert len(declared) >= 3

    sf = _FakeSF(declared)
    registry = ConfigRegistry.build(sf)

    for name, block in declared.items():
        manifest = registry.get(name)
        assert manifest is not None, f"{name} vanished from the registry"
        for key, field in _DECLARED_KEY_FIELDS.items():
            if key not in block:
                continue
            assert getattr(manifest, field) == block[key], f"{name}.{key}"
        # A second, live registration of the same graph must agree with `build`.
        again = ConfigRegistry().register_one(sf, name)
        assert again.to_dict() == manifest.to_dict()


def test_every_real_config_file_is_parsed_once_for_the_whole_build(parses):
    """The repository's own ``configs/`` directory — the 14 files / 215 817 bytes
    the 178 ms measurement was taken on — is read once, however many graphs the
    build carries."""
    real_dir = Path(config_registry.__file__).resolve().parent.parent / "configs"
    names = []
    for path in sorted(real_dir.glob("*.yaml")):
        name = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("name")
        if name:
            names.append(name)
    assert names

    sf = _FakeSF([*names, *[f"gen_extra_{i}" for i in range(20)]])
    registry = ConfigRegistry.build(sf)
    for extra in [f"gen_extra_{i}" for i in range(20)]:
        registry.register_one(sf, extra)

    expected = {path.name for path in real_dir.glob("*.yaml")}
    assert set(parses) == expected
    assert set(parses.values()) == {1}
