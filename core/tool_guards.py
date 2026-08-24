"""One guard for every caller-supplied name that becomes a path.

Three surfaces turn a name from an MCP caller or a bundle into a filesystem path:

    api/mcp_router.py     `config` → <configs>/<config>.yaml, <config>.roles.json
                          `name`   → <tools>/<name>/{tool.yaml,impl.py,README.md}
    core/pipeline_bundle.py  bundle tool keys → <tools>/<key>/…
                             config_name      → <configs>/<config>.yaml

Each grew its own check, and the checks disagreed — which is the failure this
module exists to end:

* `get_tool(name)` was guarded; `get_pipeline(config)` was not. Both are `read`
  tools, reads need no credentials, and `/mcp` is exempt from the method-based
  write gate — so `get_pipeline(config="../probe_secret")` returned the file's
  full text to an unauthenticated caller. Verified live against the running
  container: both the relative form and an absolute `config` (pathlib lets an
  absolute right-hand side REPLACE the base) read a file outside the configs dir.
* `pipeline_bundle`'s tool-name check rejected only `/`, empty and a leading dot,
  while `mcp_router`'s rejected those plus spaces, punctuation, unicode and >64
  chars. A bundle could install a tool named `my tool` that `edit_tool` and
  `get_tool` then both refuse to address — installed and uneditable.

So: one regex, one message, one importability probe, imported by both sides. A
name that reaches any of these paths is a single path segment or it is refused.
"""

from __future__ import annotations

import re

# A single path segment: no separator, no traversal, no leading dot, and short
# enough to be a directory name everywhere. Deliberately the SAME shape for tool
# names and config names — both are `<dir>/<name>.<ext>` or `<dir>/<name>/`, and
# giving them different rules is what let the two copies drift apart.
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _bad_name(name, kind: str) -> str | None:
    if not isinstance(name, str) or not SAFE_NAME.match(name or "") or ".." in name:
        return (f"invalid {kind} name {name!r} — it must be a single name "
                f"(letters, digits, dot, underscore, dash; no path separators, "
                f"no '..', no leading dot)")
    return None


def bad_tool_name(name) -> str | None:
    """Refuse a tool name that could not be a directory under the tools dir."""
    return _bad_name(name, "tool")


def bad_config_name(name) -> str | None:
    """Refuse a config name that could not be a file in the configs dir."""
    return _bad_name(name, "config")


def tool_source_error(name: str, source: str) -> str | None:
    """Import the source IN A SUBPROCESS and check it defines the tool's callable.

    A tool whose module raises on import, or which never defines `name`, registers
    fine and fails at the step that calls it — one whole run later.

    The probe runs out-of-process with a timeout because the source is written by
    an agent (or arrives inside a bundle from another machine) and is executed at
    module level: an import that blocks — a waiting loop, a network call, a heavy
    model load — would otherwise hang the calling thread with no way to cancel it,
    and any module-level side effect would land in the LIVE backend process rather
    than a throwaway one. Listing what the module does define mirrors
    `register_tool`'s own probe.
    """
    import json
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    probe = (
        "import importlib.util, json, sys\n"
        "spec = importlib.util.spec_from_file_location('probe', sys.argv[1])\n"
        "m = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(m)\n"
        "fn = getattr(m, sys.argv[2], None)\n"
        "print(json.dumps({'ok': callable(fn), 'names': sorted(\n"
        "    n for n in dir(m) if not n.startswith('_') and callable(getattr(m, n, None)))}))\n"
    )
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / f"{name}.py"
        src.write_text(source, encoding="utf-8")
        runner = Path(td) / "_probe_runner.py"
        runner.write_text(probe, encoding="utf-8")
        try:
            r = subprocess.run([sys.executable, str(runner), str(src), name],
                               capture_output=True, text=True, timeout=20, cwd=td)
        except subprocess.TimeoutExpired:
            return ("impl.py did not finish importing within 20s — module-level code "
                    "must not block (no waiting loops, no network at import time); "
                    "move that work inside the tool function")
        if r.returncode != 0:
            tail = [ln for ln in (r.stderr or "").strip().splitlines() if ln.strip()]
            return f"impl.py does not import: {tail[-1] if tail else 'no detail'}"
        try:
            info = json.loads((r.stdout or "").strip().splitlines()[-1])
        except Exception:
            return "impl.py imported but the probe produced no readable result"
    if not info.get("ok"):
        have = ", ".join(info.get("names") or []) or "nothing"
        return (f"impl.py imports but defines no callable named '{name}' — the "
                f"loader looks up the function by the tool's own name. It defines: "
                f"{have}")
    return None
