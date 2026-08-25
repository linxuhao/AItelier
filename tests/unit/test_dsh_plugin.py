"""The DSH plugin's documentation must describe the endpoint that exists.

This is the whole reason `integrations/dsh/` lives in the AItelier repo rather
than its own. The plugin's contract is not code — it is a `cordis.patch.yml` and a
tool table, and both are claims ABOUT api/mcp_router.py. In a separate repo those
claims drift the moment a tool is renamed, and nothing anywhere fails: DSH would
mount fine, the model would read a table naming a tool the server no longer has,
and the mistake surfaces as a confused agent rather than an error.

Split it out when there is a TypeScript subagent provider to release on DSH's
schedule. Until then the coupling is to this file, so the test belongs here.
"""

import json
import re
from pathlib import Path

import pytest
import yaml

from api.mcp_router import _TOOL_KIND, build_mcp

DSH = Path(__file__).resolve().parents[2] / "integrations" / "dsh"


@pytest.fixture(scope="module")
def readme() -> str:
    return (DSH / "README.md").read_text(encoding="utf-8")


class _DshLoader(yaml.SafeLoader):
    """SafeLoader that tolerates DSH's `!!js` tag.

    `!!js <expr>` is evaluated by DSH's config loader, not by YAML, so PyYAML has
    no constructor for it. Keeping the raw expression as a string is enough for the
    structural assertions here; whether the expression is a secret REFERENCE rather
    than a secret is checked against the file's text instead, where the tag is
    still visible.
    """


_DshLoader.add_multi_constructor(
    "tag:yaml.org,2002:js", lambda loader, suffix, node: f"!!js {node.value}")


@pytest.fixture(scope="module")
def patch() -> list:
    return yaml.load((DSH / "cordis.patch.yml").read_text(encoding="utf-8"),
                     Loader=_DshLoader)


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads((DSH / "package.json").read_text(encoding="utf-8"))


def _documented_tools(readme: str) -> set[str]:
    """Tool names from the README's surface table (`| `a` / `b` | kind | …`)."""
    names: set[str] = set()
    for line in readme.splitlines():
        if not line.startswith("| `"):
            continue
        cell = line.split("|")[1]
        names.update(re.findall(r"`([a-z_]+)`", cell))
    return names


def test_the_readme_documents_exactly_the_tools_the_server_serves(readme):
    build_mcp()
    documented = _documented_tools(readme)
    assert documented, "parsed no tool table out of the README"
    assert documented == set(_TOOL_KIND), (
        f"only in README: {sorted(documented - set(_TOOL_KIND))}; "
        f"only in server: {sorted(set(_TOOL_KIND) - documented)}")


def test_each_tool_is_documented_with_the_authority_it_actually_has(readme):
    """A read documented as a write teaches the operator to over-provision the
    token; a write documented as a read teaches them it needs no credential."""
    build_mcp()
    for line in readme.splitlines():
        if not line.startswith("| `"):
            continue
        cells = [c.strip() for c in line.split("|")]
        for name in re.findall(r"`([a-z_]+)`", cells[1]):
            assert _TOOL_KIND[name] == cells[2], (
                f"{name}: README says {cells[2]!r}, server says {_TOOL_KIND[name]!r}")


def test_the_patch_mounts_the_mcp_client_at_the_documented_prefix(patch, readme):
    row = patch[0]["insert"][0]
    assert row["name"] == "@deepseek-ai/dsh-mcp-client"
    server = row["config"]["serverName"]
    # dsh-mcp-client namespaces the model-facing names as mcp__<serverName>__<tool>,
    # so the prefix the README teaches is a consequence of this field.
    assert f"mcp__{server}__" in readme


def test_the_patch_carries_a_reference_to_the_token_and_never_a_token(patch):
    raw = (DSH / "cordis.patch.yml").read_text(encoding="utf-8")
    assert "!!js process.env.AITELIER_ADMIN_TOKEN" in raw
    # A literal secret in a file that ships to npm is the failure this guards.
    for line in raw.splitlines():
        if "TOKEN" in line and "!!js" not in line and not line.lstrip().startswith("#"):
            pytest.fail(f"token line without an env reference: {line!r}")


def test_the_manifest_declares_itself_a_dsh_bundle(manifest):
    assert manifest["dsh"]["bundle"]["patch"] == "./cordis.patch.yml"
    # DSH asks plugins to carry this topic/keyword for discoverability.
    assert "dsh-plugin" in manifest["keywords"]
    assert "cordis.patch.yml" in manifest["files"], (
        "the patch is the whole plugin — leaving it out of `files` publishes an "
        "empty package that installs and does nothing")


def test_the_root_readme_tool_count_matches_the_server():
    """The product README claims a literal tool count ("17 tools"). A count is the
    cheapest claim to let drift: add a tool and the sentence silently understates
    the surface. Same rule as the table test above — a doc that is a claim about
    api/mcp_router.py gets bound to it."""
    build_mcp()
    root_readme = (DSH.parents[1] / "README.md").read_text(encoding="utf-8")
    m = re.search(r"\*\*(\d+) tools\*\*", root_readme)
    assert m, "the root README no longer states the MCP tool count"
    assert int(m.group(1)) == len(_TOOL_KIND), (
        f"README says {m.group(1)} tools, server has {len(_TOOL_KIND)}")


# ── The shipped skill ────────────────────────────────────────────────────────

def test_the_skill_ships_in_the_package(manifest):
    """A skill outside `files` is not in the tarball, so the install command in
    the README would copy a directory that npm never delivered."""
    assert "skills" in manifest["files"]
    assert (DSH / "skills" / "aitelier-pipelines" / "SKILL.md").is_file()


def test_the_skill_declares_the_frontmatter_dsh_indexes_on():
    """DSH's catalog renders `name` + `description`; a skill missing either is
    discovered but unselectable, which reads as "the skill does nothing"."""
    body = (DSH / "skills" / "aitelier-pipelines" / "SKILL.md").read_text(encoding="utf-8")
    assert body.startswith("---\n")
    front = body.split("---", 2)[1]
    assert "name: aitelier-pipelines" in front
    desc = next(l for l in front.splitlines() if l.startswith("description:"))
    # DSH's own skills open with "Use when …" so the model can route on it.
    assert "Use when" in desc


def test_the_skill_only_names_tools_the_server_actually_serves():
    """The skill tells an agent which tool answers which question. A tool it
    names that does not exist sends the agent looking for a surface that is not
    there — the same drift the README table test guards, one layer up."""
    import re
    body = (DSH / "skills" / "aitelier-pipelines" / "SKILL.md").read_text(encoding="utf-8")
    named = set(re.findall(r"`([a-z_]{4,})\(", body)) | set(
        re.findall(r"`(get_run_summary|list_pipelines|trace_list|trace_read)`", body))
    unknown = {t for t in named if t not in _TOOL_KIND}
    assert not unknown, f"the skill names tools the server does not serve: {sorted(unknown)}"


def test_the_readme_tells_the_reader_how_to_install_the_skill(readme):
    """Shipping a skill nobody is told to install is shipping nothing."""
    assert "aitelier-pipelines" in readme
    assert "node_modules/dsh-plugin-aitelier/skills" in readme


def test_the_inbound_and_outbound_mcp_urls_do_not_share_a_name():
    """`AITELIER_MCP_URL` is public API now — the published plugin teaches it as
    "where DSH finds AItelier". AItelier's own outbound media client must not
    read the same name, or setting it for the plugin points the media tools at
    AItelier itself, where none of those tools exist."""
    src = (DSH.parents[1] / "aitelier" / "mcp_client.py").read_text(encoding="utf-8")
    import re
    read = set(re.findall(r'os\.environ\.get\("([A-Z_]+)"', src))
    assert "AITELIER_MCP_URL" not in read
    assert "AITELIER_MEDIA_MCP_URL" in read
