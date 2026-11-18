"""The CLI funnel turns an unusable credential into a sentence, not a traceback.

MEASURED defect (iss-bd95464ca82d4701, main a8e75fad95): `AITELIER_DRIVER_ID`
exported without its token file raised CredentialError inside
``cli/client.py::_auth_headers`` (called from ``APIClient.__init__``) and nothing
in ``cli/`` caught it, so every command died with a traceback instead of the one
sentence that names the file to fix.

The witness drives ``cli.app._entrypoint`` itself -- the single funnel
``pyproject.toml`` installs as the ``aitelier`` console script -- with a real
credential environment and a real token file that is absent or group-readable.
Only the backend start is stubbed: the defect lives in credential resolution,
which runs before any request is sent.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from cli import app as cli_app

CREDENTIAL_ENV = (
    "AITELIER_DRIVER_TOKEN",
    "AITELIER_DRIVER_TOKEN_FILE",
    "AITELIER_ADMIN_TOKEN",
)


@pytest.fixture
def oneshot(monkeypatch):
    """The one-shot path reaches _get_client() without needing a TTY."""
    monkeypatch.setattr(sys, "argv", ["aitelier", "--oneshot", "hello"])
    monkeypatch.setattr("cli.server.ensure_server_running", lambda url=None: None)


@pytest.fixture
def driver_environment(tmp_path, monkeypatch):
    """A driver id whose token file resolves under a directory of its own."""
    for name in CREDENTIAL_ENV:
        monkeypatch.delenv(name, raising=False)
    drivers = tmp_path / "drivers"
    monkeypatch.setenv("AITELIER_DRIVER_TOKENS_DIR", str(drivers))
    monkeypatch.setenv("AITELIER_DRIVER_ID", "dsh-bogus-no-file")
    return drivers


def test_a_driver_id_without_its_token_file_exits_with_a_sentence(
    oneshot, driver_environment, capsys,
):
    with pytest.raises(SystemExit) as raised:
        cli_app._entrypoint()

    assert raised.value.code == 2, "a configuration error is not a usage error"
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert captured.err.startswith("Error: "), captured.err
    assert "driver token file" in captured.err
    assert "dsh-bogus-no-file.token" in captured.err


def test_a_group_readable_token_file_names_the_chmod_and_never_the_token(
    oneshot, driver_environment, capsys,
):
    secret = "s3cr3t-driver-token-value"
    token_file = driver_environment / "dsh-bogus-no-file.token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text(secret + "\n", encoding="utf-8")
    token_file.chmod(0o644)

    with pytest.raises(SystemExit) as raised:
        cli_app._entrypoint()

    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "must be mode 0600" in captured.err
    assert f"chmod 600 {token_file}" in captured.err
    # The refusal quotes the file, never the credential it holds.
    assert secret not in captured.out
    assert secret not in captured.err


def test_the_funnel_is_the_installed_console_script():
    """If the script stops pointing at _entrypoint, the sentence stops appearing."""
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    assert 'aitelier = "cli.app:_entrypoint"' in pyproject.read_text(encoding="utf-8")
