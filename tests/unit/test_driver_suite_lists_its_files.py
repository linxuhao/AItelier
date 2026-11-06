"""A second observer for the driver suite's own composition.

`integrations/dsh-driver/suite-composition.test.mjs` guards two rules and cannot
guard the one that matters most about itself: if it is dropped from the npm test
script it simply does not run, and `npm test` is green. A non-author review measured
exactly that - delete the file from the script and the suite exits 0 with 101/101
passing - which is the omission its own message promises to catch ("a file nobody
runs is a guard nobody has").

So the check lives here too, in a different runtime, collected by a different runner:
pytest reaches this file whether or not any javascript ran. It is written from the
rules rather than copied from the javascript, because the second review found that a
copied predicate fails on the same input as the original - the two are only worth
having if they can disagree.

The chain ends here. If this file is deleted as well, nothing notices. That is
written down rather than implied, and it is why nothing here is clever.

Nothing here executes the javascript: it reads names and text.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DRIVER = ROOT / "integrations" / "dsh-driver"
# Node's discovery is wider than *.test.* / *.spec.*: it also runs `*-test.mjs`,
# `*_test.mjs`, `test-*.mjs` and test.*. Written from the rules here rather than
# copied from the javascript, so the two can disagree - they already have.
SUFFIX = re.compile(r"\.(mjs|cjs|js)$", re.IGNORECASE)


def _is_test_file(name: str) -> bool:
    base = name.lower()
    if not SUFFIX.search(base):
        return False
    return (
        ".test." in base
        or ".spec." in base
        or "-test." in base
        or "_test." in base
        or base.startswith("test-")
        or base in {"test.js", "test.mjs", "test.cjs"}
    )
GUARD = "suite-composition.test.mjs"
# The guard's own rules, by the names of the tests it declares. A file gutted to
# `test.skip(...)` stays listed and would otherwise be green in both observers.
GUARD_RULES = ("every test file the runner would find is run by npm test",
               "only the resolver and its unit test resolve the DSH entry")


def _code_only(body: str) -> str:
    """Drop comment lines: this file's own prose names the tokens it looks for."""
    kept, in_block = [], False
    for line in body.splitlines():
        stripped = line.strip()
        if in_block:
            if "*/" in stripped:
                in_block = False
            continue
        if stripped.startswith("/*"):
            if "*/" not in stripped:
                in_block = True
            continue
        if stripped.startswith("//"):
            continue
        kept.append(line)
    return "\n".join(kept)


def _script() -> str:
    pkg = json.loads((DRIVER / "package.json").read_text(encoding="utf-8"))
    return str(pkg.get("scripts", {}).get("test", "")).strip()


def _test_files() -> list[str]:
    return sorted(
        path.relative_to(DRIVER).as_posix()
        for path in DRIVER.rglob("*")
        if path.is_file()
        and _is_test_file(path.name)
        and "node_modules" not in path.parts
    )


def test_the_guard_that_cannot_see_its_own_removal_is_listed_and_not_gutted() -> None:
    script = _script()
    assert GUARD in script, (
        f"{GUARD} is not in integrations/dsh-driver's npm test script. It is the only thing keeping "
        "the hand-rolled DSH-entry expression and the unlisted-test-file rule from decaying, and it "
        "cannot report its own absence - which is why this test exists."
    )
    body = _code_only((DRIVER / GUARD).read_text(encoding="utf-8"))
    missing = [rule for rule in GUARD_RULES if rule not in body]
    assert not missing, (
        f"{GUARD} no longer declares: {missing}. A guard that is still listed but gutted to "
        "test.skip() is green in both observers while checking nothing."
    )
    assert "test.skip(" not in body, f"{GUARD} skips a test; a skipped guard is a guard that is off."


def test_the_script_names_every_test_file_and_cannot_be_faked() -> None:
    script = _script()
    # Same two shapes the javascript guard accepts, checked independently: a chain or a
    # pipe can make a failing suite exit 0, and a glob can run fewer files than it
    # looks like (review 2, leak 1: one asterisk silenced both observers).
    assert not re.search(r"[;&|]", script), (
        f"the test script chains commands or swallows failures: {script!r}. A suite that cannot "
        "fail is a suite that cannot report."
    )
    assert script.startswith("node --test"), f"the test script must be a `node --test` invocation: {script!r}"
    args = [a for a in script[len("node --test"):].split() if a]
    globs = [a for a in args if re.search(r"[*?\[\]]", a)]
    assert not globs, (
        f"the test script passes a glob: {globs}. A partial glob runs fewer files than it looks "
        "like: name every test file, or use bare `node --test`."
    )
    present = _test_files()
    assert present, "found no *.test.* files under integrations/dsh-driver - the discovery is broken"
    if not args:
        return  # bare discovery runs whatever exists
    missing = [name for name in present if name not in script]
    assert not missing, (
        f"npm test does not run: {', '.join(missing)}. A test file nobody runs is a guard nobody "
        "has, and the javascript guard cannot see this either when it is one of the missing files."
    )
