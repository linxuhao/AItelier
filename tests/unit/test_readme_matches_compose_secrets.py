"""The README's touch-list and docker-compose's secrets block must name the same files.

This is not documentation polish. The compose file grew a fifth secret
(`QWEN_API_KEY`) after the README's touch-list was written naming four, and a
clean-machine install that followed the README verbatim was refused by Docker
at `up -d` — `bind source path does not exist` — 2026-08-27, on the first
stranger-simulation install ever run against the public repo. The two lists
live in different files, so nothing but a test makes them move together.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _compose_secret_files() -> set[str]:
    text = (ROOT / "docker-compose.yml").read_text()
    # Every secret is declared as `file: ${HOME}/.aitelier-secrets/<NAME>`.
    names = set(re.findall(r"\.aitelier-secrets/([A-Z0-9_]+)", text))
    assert names, "no secrets found in docker-compose.yml — pattern rot?"
    return names


def _readme_touch_names() -> set[str]:
    text = (ROOT / "README.md").read_text()
    m = re.search(r"cd ~/\.aitelier-secrets && touch ([A-Z0-9_ ]+?) && chmod", text)
    assert m, "README no longer has the touch-list line this test pins"
    return set(m.group(1).split())


def test_readme_touch_list_names_every_compose_secret():
    assert _readme_touch_names() == _compose_secret_files()
