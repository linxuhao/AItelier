"""Two pytest instruments may no longer disagree about pytest.ini in silence.

`pytest.ini` switches such as `asyncio_mode` are registered by PLUGINS, not by
pytest itself. An interpreter without the plugin does not fail — pytest emits a
`PytestConfigWarning`, ignores the switch, and every test the switch was meant
to enable then fails for a reason that reads like a code defect. Measured
2026-09-20 on c51497bb: the aitelier image (no pytest-asyncio) reported 209
failed where the host gate venv (pytest-asyncio 1.4.0) reported 4 failed, and
the two sets were disjoint. 206 of the image's reds were `async def` tests the
missing plugin could not run. Nothing anywhere raised an alarm.

This check is that alarm, and it runs inside whichever interpreter is executing
the suite — so the instrument that is wrong is the one that goes red.
"""
import configparser
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTEST_INI = ROOT / "pytest.ini"

# Which distribution registers a switch, so the failure can name it. A switch
# that is not listed here still fails the check; it is only named without its
# plugin.
PLUGIN_BY_INI_OPTION = {
    "asyncio_mode": "pytest-asyncio",
    "asyncio_default_fixture_loop_scope": "pytest-asyncio",
    "asyncio_default_test_loop_scope": "pytest-asyncio",
    "anyio_mode": "anyio",
    "trio_mode": "pytest-trio",
    "DJANGO_SETTINGS_MODULE": "pytest-django",
    "timeout": "pytest-timeout",
}


def _declared_ini_options() -> list:
    parser = configparser.ConfigParser()
    parser.optionxform = str  # pytest ini keys are case-sensitive
    parser.read(PYTEST_INI, encoding="utf-8")
    return list(parser["pytest"])


def test_every_pytest_ini_switch_is_registered_in_this_interpreter(pytestconfig):
    # `Config.getini` raises ValueError for a name no loaded plugin registered,
    # which is stricter than "the module imports": it proves the plugin is
    # actually active in the process reading pytest.ini.
    unregistered = []
    for option in _declared_ini_options():
        try:
            pytestconfig.getini(option)
        except ValueError:
            plugin = PLUGIN_BY_INI_OPTION.get(option, "an unidentified plugin")
            unregistered.append(f"{option} (registered by {plugin})")

    assert not unregistered, (
        "pytest.ini turns on switches this interpreter's pytest does not "
        "recognise, so they are silently ignored here while another instrument "
        "honours them. Missing plugin(s) for: " + "; ".join(unregistered)
    )


def test_an_async_test_actually_runs_under_the_configured_mode(tmp_path):
    """Closes iss-0d657d1ab5004494 item 1.

    A plugin that registers `asyncio_mode` but no longer supports its value
    keeps the registration check above green while the async tests silently
    stop running. This runs a real `async def` test in a subprocess under the
    repository's own pytest.ini and demands that it genuinely passed.
    """
    module = tmp_path / "test_async_probe.py"
    module.write_text(
        textwrap.dedent(
            """\
            async def test_runs_as_a_coroutine():
                assert True
            """
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(PYTEST_INI),
            "--rootdir",
            str(ROOT),
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(tmp_path / "basetemp"),
            "-q",
            str(module),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    summary = lines[-1].lower() if lines else ""

    assert result.returncode == 0, (
        f"pytest exited {result.returncode} on a lone async test:\n{output}"
    )
    assert "1 passed" in summary, (
        f"summary line {summary!r} does not report 1 passed:\n{output}"
    )
    for forbidden in ("skipped", "failed", "error", "warning"):
        assert forbidden not in summary, (
            f"summary line {summary!r} contains {forbidden!r}:\n{output}"
        )
