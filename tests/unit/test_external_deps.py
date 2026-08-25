"""An error that does not name its config is not actionable.

`godot-builder unreachable (http://godot-builder:8080)` is a true sentence that
never says what to edit. Every capability backed by something outside this repo
must name its CONFIG KEY when it refuses, and the doc must describe the same key
the code actually reads — which is why both come from one table.
"""
import re
from pathlib import Path

import pytest

from core import external_deps as ed

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "external-dependencies.md"


class TestTheTableIsWellFormed:
    def test_keys_are_unique(self):
        assert len({d.key for d in ed.DEPS}) == len(ed.DEPS)

    def test_every_dep_says_what_breaks_and_how_to_fix_it(self):
        for d in ed.DEPS:
            assert d.capability and d.resource, d.key
            assert d.how, f"{d.key}: no way to configure it is stated"
            assert d.without, f"{d.key}: does not say what happens without it"


class TestEveryMessageNamesItsConfig:
    @pytest.mark.parametrize("dep", ed.DEPS, ids=lambda d: d.key)
    def test_missing_names_the_key(self, dep):
        assert dep.key in ed.missing(dep.key)

    @pytest.mark.parametrize("dep", ed.DEPS, ids=lambda d: d.key)
    def test_unreachable_names_the_key(self, dep):
        assert dep.key in ed.unreachable(dep.key, "http://x", "boom")

    @pytest.mark.parametrize("dep", ed.DEPS, ids=lambda d: d.key)
    def test_both_say_which_capability_is_lost(self, dep):
        """"KEY is not set" alone makes the reader work out what they lost."""
        head = dep.capability.split(" —")[0].split(" (")[0]
        assert head in ed.missing(dep.key)
        assert head in ed.unreachable(dep.key, "http://x")

    def test_missing_and_unreachable_are_not_the_same_sentence(self):
        """Different fixes: nothing configured vs configured and not answering.
        Conflating them sends the reader to edit a correct variable."""
        k = "GODOT_BUILDER_URL"
        assert ed.missing(k) != ed.unreachable(k, "http://x")
        assert "not set" in ed.missing(k)
        assert "did not answer" in ed.unreachable(k, "http://x")

    def test_a_default_is_called_a_default(self):
        """"GODOT_BUILDER_URL=http://godot-builder:8080" reads as a choice the
        deployment made; at its default it is a choice nobody made."""
        d = ed.BY_KEY["GODOT_BUILDER_URL"]
        assert "at its default" in ed.unreachable(d.key, d.default)
        assert "at its default" not in ed.unreachable(d.key, "http://elsewhere")

    def test_an_unregistered_key_still_produces_a_usable_line(self):
        out = ed.missing("SOME_FUTURE_KEY")
        assert "SOME_FUTURE_KEY" in out


class TestTheDocAndTheCodeCannotDrift:
    def test_every_dep_appears_in_the_doc(self):
        body = DOC.read_text(encoding="utf-8")
        missing = [d.key for d in ed.DEPS if d.key not in body]
        assert not missing, (
            f"{missing} is read by the code but absent from {DOC.name}. "
            f"It is generated from core.external_deps.DEPS — regenerate it.")

    def test_the_doc_names_no_config_the_table_does_not_have(self):
        """A doc line for a variable nothing reads is worse than no line: the
        reader sets it and nothing changes."""
        body = DOC.read_text(encoding="utf-8")
        table = body[body.index("| Capability"):body.index("## Which LLM provider")]
        cited = set(re.findall(r"`([A-Z][A-Z0-9_]{4,})`", table))
        assert cited <= set(ed.BY_KEY), sorted(cited - set(ed.BY_KEY))


class TestProviderAgnostic:
    """AItelier does not depend on DeepSeek, or on Ark. It depends on SOME
    provider, named in llm_providers.json — which is data, not code. Baking two
    vendors into the dependency table stated the opposite of the design."""

    def test_no_vendor_key_is_hard_coded_in_the_table(self):
        hard = [d.key for d in ed.DEPS if d.key.endswith("_API_KEY")]
        assert hard == [], (
            f"{hard} are provider keys pinned into DEPS. They belong to "
            f"llm_providers.json entries, which anyone may replace.")

    def test_every_registered_provider_key_resolves(self):
        import json
        providers = json.loads(
            (ROOT / "llm_providers.json").read_text(encoding="utf-8"))
        for name, cfg in providers.items():
            key = cfg["api_key_env"]
            dep = ed.resolve(key)
            assert dep is not None, f"{name}: {key} produces no message"
            assert name in ed.missing(key) and key in ed.missing(key)

    def test_a_provider_added_later_gets_the_same_message(self, tmp_path,
                                                          monkeypatch):
        """The whole point: nothing here enumerates vendors, so a provider
        someone adds tomorrow is described exactly like the shipped ones."""
        import json
        reg = ROOT / "llm_providers.json"
        original = reg.read_text(encoding="utf-8")
        data = json.loads(original)
        data["acme"] = {"base_url": "https://api.acme.test/v1",
                        "api_key_env": "ACME_API_KEY"}
        try:
            reg.write_text(json.dumps(data), encoding="utf-8")
            out = ed.missing("ACME_API_KEY")
        finally:
            reg.write_text(original, encoding="utf-8")
        assert "ACME_API_KEY" in out and "acme" in out
        assert "https://api.acme.test/v1" in out

    def test_the_doc_explains_the_registry_rather_than_two_vendors(self):
        body = DOC.read_text(encoding="utf-8")
        assert "provider-agnostic" in body
        assert "llm_providers.json" in body


class TestTheMediaServerHoldsState:
    """It is not just models and a GPU — it holds the CAST, and the cast is what
    keeps a character looking and sounding like itself between runs. Swapping
    servers mid-project silently recasts everyone."""

    def test_what_the_server_holds_says_cast(self):
        """Asserted on `resource` ALONE: an `or` across two fields lets one of
        them carry the test, and then removing the other passes."""
        assert "cast" in ed.BY_KEY["AITELIER_MEDIA_MCP_URL"].resource.lower()

    def test_the_consequence_of_repointing_it_is_stated(self):
        """"asset generation refuses" is the small half. The expensive half is
        that a project already half-drawn comes back with different faces."""
        without = ed.BY_KEY["AITELIER_MEDIA_MCP_URL"].without.lower()
        assert "recast" in without

    def test_the_doc_carries_that_warning_too(self):
        body = DOC.read_text(encoding="utf-8")
        assert "recast" in body


class TestTheCallersUseIt:
    """A table nothing calls is documentation pretending to be a mechanism."""

    @pytest.mark.parametrize("path,key", [
        ("core/web_tools.py", "SEARXNG_URL"),
        ("aitelier/mcp_client.py", "AITELIER_MEDIA_MCP_URL"),
        ("aitelier/tools/godot_compile/impl.py", "GODOT_BUILDER_URL"),
        ("aitelier/tools/godot_playtest/impl.py", "GODOT_BUILDER_URL"),
        ("aitelier/tools/gdscript_check/impl.py", "GODOT_BUILDER_URL"),
    ])
    def test_the_caller_reports_through_the_table(self, path, key):
        src = (ROOT / path).read_text(encoding="utf-8")
        assert "external_deps" in src, f"{path} does not use the shared table"
        assert key in src

    def test_a_provider_with_no_key_fails_naming_the_secret(self):
        """The one that matters most: without it a missing key surfaces as the
        provider's own 401, several layers away, naming nothing."""
        from core.ai_router import AIGateway
        gw = AIGateway.__new__(AIGateway)
        gw.api_key = None
        gw.missing_key_env = "DEEPSEEK_API_KEY"
        gw.provider = "deepseek"
        gw.litellm_model = "openai/deepseek-v4"
        assert "DEEPSEEK_API_KEY" in ed.missing(
            gw.missing_key_env, f"Model '{gw.litellm_model}' resolves to "
                                f"provider '{gw.provider}', which reads it.")


class TestTheReadmeMatchesTheShippedConfigs:
    """The install page's first instruction is "get this key". Naming the wrong
    one costs a new user their entire first run — every step fails at a provider
    they were never told to sign up with. It went stale exactly that way: the
    agent configs moved to `ark/`, the README kept saying DeepSeek."""

    def _shipped_providers(self):
        import re
        provs = set()
        for f in (ROOT / "agent_configs").glob("*.yaml"):
            for m in re.finditer(r'^\s+model:\s*"?([a-z0-9_]+)/',
                                 f.read_text(encoding="utf-8"), re.M):
                provs.add(m.group(1))
        return provs

    def test_the_readme_names_a_key_the_shipped_configs_actually_use(self):
        import json
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        quick = readme[readme.index("## Quick Start"):readme.index("## Architecture")]
        providers = json.loads(
            (ROOT / "llm_providers.json").read_text(encoding="utf-8"))
        wanted = {providers[p]["api_key_env"] for p in self._shipped_providers()
                  if p in providers}
        assert wanted, "no provider prefix found in agent_configs"
        # BOTH directions. `any(...)` alone was satisfied by a second mention
        # further down the section, so renaming the headline key stayed green —
        # the same or-across-sources hole this file's own guards had.
        assert any(k in quick for k in wanted), (
            f"Quick Start names none of {sorted(wanted)} — the keys the shipped "
            f"agent_configs actually need.")
        # Only the lines that say "write your real key here" — `touch A B C`
        # legitimately names every secret FILE compose mounts, whichever
        # provider you picked, and flagging those would be noise.
        # Per LINE, not through the printf arguments: the placeholder is
        # literally `<your-key>`, so a regex that treats `>` as the redirect
        # stops inside it and matches nothing — this guard passed vacuously
        # until a mutation that should have killed it did not.
        told_to_obtain = {m.group(1)
                          for line in quick.splitlines() if "printf" in line
                          for m in [re.search(
                              r"aitelier-secrets/([A-Z][A-Z0-9_]*_API_KEY)", line)]
                          if m}
        stale = told_to_obtain - wanted
        assert not stale, (
            f"Quick Start tells a new user to get {sorted(stale)}, which no "
            f"shipped agent_config uses. Their first run fails at a provider "
            f"they were never told to sign up with. Needed: {sorted(wanted)}.")

    def test_the_host_model_default_is_documented_correctly(self):
        from core.agents import HOST_AGENT_MODEL
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        line = next(l for l in readme.splitlines()
                    if "AITELIER_HOST_AGENT_MODEL" in l and "default" in l)
        assert HOST_AGENT_MODEL in line, f"README says: {line.strip()[:120]}"

    def test_the_readme_does_not_tell_you_to_put_a_key_in_env(self):
        """.env.example says "DOCKER: do NOT put this key here"; the README used
        to say the opposite two paragraphs earlier. Contradicting docs on step
        one are worse than one silent doc."""
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        quick = readme[readme.index("## Quick Start"):readme.index("## Architecture")]
        assert "add DEEPSEEK_API_KEY to .env" not in quick
        assert "aitelier-secrets" in quick
