"""The secrets dir is mounted WHOLE — compose must never enumerate key names again.

History, because this test replaces one that pointed the opposite way: compose
used to enumerate five Docker secrets, the README's touch-list said four, and
the first stranger install (2026-08-27) died at `up -d` on the drift. The first
fix pinned the two lists together; the real fix (same day) removed the
enumeration entirely — the whole `~/.aitelier-secrets` dir is mounted and
`ai_router._read_secret` resolves `$AITELIER_SECRETS_DIR/<name>`, so the set of
key names is owned by the provider tables alone. This test pins THAT: if a
per-key `secrets:` enumeration creeps back into compose, the drift class it
enables comes back with it.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = (ROOT / "docker-compose.yml").read_text()


def test_compose_mounts_the_whole_secrets_dir():
    assert "/.aitelier-secrets:/run/aitelier-secrets:ro" in COMPOSE
    assert "AITELIER_SECRETS_DIR: /run/aitelier-secrets" in COMPOSE


def test_compose_enumerates_no_per_key_secrets():
    # A top-level `secrets:` block or a service-level `secrets:` list is the
    # enumeration pattern this repo left behind.
    for line in COMPOSE.splitlines():
        assert not line.rstrip().endswith("secrets:") or "aitelier-secrets" in line, (
            f"per-key secrets enumeration is back: {line!r}")


def test_credential_helper_follows_the_mounted_dir():
    """git-credential-helper.sh reads a path env; compose must point it at the mount."""
    assert "AITELIER_GITHUB_TOKEN_FILE: /run/aitelier-secrets/GITHUB_TOKEN" in COMPOSE
