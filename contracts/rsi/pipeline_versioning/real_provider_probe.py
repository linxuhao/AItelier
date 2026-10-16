"""Diagnostic binding to accepted frozen-bundle APIs, NOT a conforming store.

The frozen r1 generic bundle/run API has no faithful mapping to registered
SkillFlow inputs. This probe retains native refusals, not fake history or pins.
Pure codec/error definitions are shared with the unchanged statement.
"""
import copy
from pathlib import Path
from fake import (canonical_bytes, version_id_of, _check_id, _check_run_id,
                  _text_bytes, UnknownVersion, UnknownRun, NoCurrentVersion,
                  Conflict, MalformedBundle)
from core import pipeline_frozen_bundle as fb
from core.config_registry import ConfigRegistry
from core.skillflow_host import AItelierSkillFlow
from skillflow import PipelineGraph


class ProviderBoundary(Exception):
    code = "PROVIDER_DOMAIN_MISMATCH"


class RealProviderProbe:
    capabilities = frozenset()  # no tamper hook in the real provider

    def __init__(self, root, available_tools):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.gen = self.root / "gen"
        self.gen.mkdir()
        self.sf = AItelierSkillFlow(str(self.root / "sf.db"),
                                   workspace_base=str(self.root / "ws"),
                                   projects_base=str(self.root / "projects"))
        self.registry = ConfigRegistry()
        # A host owns its actual provider inventory. These synthetic tags are
        # input evidence, never installed or asserted as real tool identities.
        self.requested_inventory = copy.deepcopy(available_tools)

    canonical_form = staticmethod(canonical_bytes)
    version_id_of = staticmethod(version_id_of)

    def capture(self, bundle, **kwargs):
        canonical_bytes(bundle)  # unchanged protocol validity/canonical rules
        try:
            graph = PipelineGraph._from_dict(copy.deepcopy(bundle["graph"]))
        except Exception as exc:
            raise ProviderBoundary("actual PipelineGraph parser: " +
                                   type(exc).__name__ + ": " + str(exc)) from exc
        # No generic nodes/edges->steps rewrite or synthetic graph publication.
        return fb.capture(self.sf, self.registry, graph.name, configs_dirs=[self.gen])

    def list_versions(self):
        return [{"version_id": "sha256:" + x["bundle_identity"],
                 "physical_name": x["physical_name"]}
                for x in (fb.verify_sealed(self.gen, f.name.removesuffix(".bundle.json"))
                          for f in sorted(self.gen.glob("*.bundle.json")))]

    def current_version(self):
        if not (self.gen / "gen_contract.yaml").exists():
            return None
        return "sha256:" + fb.unit_identity(fb.read_unit(self.gen, "gen_contract"))

    def read(self, version_id):
        _check_id(version_id)
        for row in self.list_versions():
            if row["version_id"] == version_id:
                return fb.verify_sealed(self.gen, row["physical_name"])
        raise UnknownVersion("no actual filesystem seal has that bundle identity")

    def restore(self, version_id, **kwargs):
        self.read(version_id)
        raise ProviderBoundary("native restore requires a registered target and expected whole-unit identity")

    def pin_run(self, run_id):
        _check_run_id(run_id)
        if self.current_version() is None:
            raise NoCurrentVersion("no registered current unit")
        self.run_pin(run_id)
        raise ProviderBoundary("SDK already pins at create_run; pin_run cannot invent a run")

    def run_pin(self, run_id):
        _check_run_id(run_id)
        if self.sf.get_run(run_id) is None:
            raise UnknownRun("no actual SDK run with this id")
        raise ProviderBoundary("graph_version alone is not a whole-bundle pin")

    def finish_run(self, run_id):
        self.run_pin(run_id)
        raise ProviderBoundary("finishing requires actual SDK lifecycle, not a pin-state assignment")

    def list_pins(self):
        # Native runs, not an alias or pin dictionary. Vectors create none:
        # their first valid capture is outside the registered graph domain.
        return {r["id"]: {"graph_version": r["graph_version"], "status": r["status"]}
                for r in self.sf.list_runs()}

    def set_available_tools(self, tools):
        if not isinstance(tools, dict):
            raise MalformedBundle("tool inventory must be a mapping")
        for name, identity in tools.items():
            _text_bytes(name, "tool name")
            _text_bytes(identity, "tool identity")
            if not name or not identity:
                raise MalformedBundle("empty tool identity")
        raise ProviderBoundary("synthetic tags cannot replace the actual host ToolLoader inventory")

    def close(self):
        self.sf._conn.close()
