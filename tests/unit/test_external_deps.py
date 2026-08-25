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
        table = body[body.index("| Capability"):body.index("## What you actually")]
        cited = set(re.findall(r"`([A-Z][A-Z0-9_]{4,})`", table))
        assert cited <= set(ed.BY_KEY), sorted(cited - set(ed.BY_KEY))


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
