"""A second observer for the driver suite's own composition.

`integrations/dsh-driver/suite-composition.test.mjs` guards two rules and cannot
guard the one that matters most about itself: if it is dropped from the npm test
script, it simply does not run, and `npm test` is green. A non-author review
measured exactly that on 2026-11-05 - delete the file from the script and the
suite exits 0 with 101/101 passing - which is the omission its own message
promises to catch ("a file nobody runs is a guard nobody has").

So the check lives here too, in a different runtime, collected by a different
runner: pytest reaches this file whether or not any javascript ran. It is
deliberately small and boring. If this file is deleted as well, nothing notices;
that is the end of the chain, and it is written down rather than implied.

Nothing here executes the javascript: it compares names.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DRIVER = ROOT / "integrations" / "dsh-driver"
TEST_FILE = re.compile(r"\.(test|spec)\.(mjs|cjs|js)$", re.IGNORECASE)
GUARD = "suite-composition.test.mjs"


def _script() -> str:
    return str(json.loads((DRIVER / "package.json").read_text(encoding="utf-8")).get("scripts", {}).get("test", ""))


def _test_files() -> list[str]:
    return sorted(
        str(path.relative_to(DRIVER))
        for path in DRIVER.rglob("*")
        if path.is_file() and TEST_FILE.search(path.name) and "node_modules" not in path.parts
    )


def test_the_guard_that_cannot_see_its_own_removal_is_listed_here() -> None:
    script = _script()
    assert GUARD in script, (
        f"{GUARD} is not in integrations/dsh-driver's npm test script. That file is the only thing "
        "keeping the hand-rolled DSH-entry expression and the unlisted-test-file rule from decaying, "
        "and it cannot report its own absence - which is why this test exists."
    )


def test_every_driver_test_file_is_in_the_npm_test_script() -> None:
    script = _script()
    present = _test_files()
    assert present, "found no *.test.* files under integrations/dsh-driver - the discovery is broken"
    # A glob or bare `node --test` discovers files itself, so comparing names would
    # be wrong rather than strict.
    discovers = "--test" in script and ("*" in script or "?" in script or not TEST_FILE.search(script))
    if discovers:
        return
    missing = [name for name in present if name not in script]
    assert not missing, (
        f"npm test does not run: {', '.join(missing)}. A test file nobody runs is a guard nobody has, "
        "and the javascript guard cannot see this either when it is one of the missing files."
    )
