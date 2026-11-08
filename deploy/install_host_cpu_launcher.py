#!/usr/bin/env python3
"""Install the supervised host CPU launcher unit and its pre-start probe.

Renders deploy/aitelier-host-cpu-launcher.service with this host's absolute
paths and writes it plus deploy/cpu-launcher-preexec.py under a HOME-shaped
target root. Nothing is enabled, started or reloaded unless --enable is given:
by default the installer touches no service and runs no systemctl.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

SERVICE_NAME = "aitelier-host-cpu-launcher.service"
# The template and probe are read from the checkout this script lives in; the
# rendered values (--repo-root) may name a different installed checkout.
CHECKOUT = Path(__file__).resolve().parents[1]
TEMPLATE = CHECKOUT / "deploy/aitelier-host-cpu-launcher.service"
PROBE = CHECKOUT / "deploy/cpu-launcher-preexec.py"
UNREPLACED = re.compile(r"@[A-Z_]+@")


def atomic_text(path: Path, content: str, mode: int) -> bool:
    """Replace ``path`` atomically; return whether its content changed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text() == content:
        path.chmod(mode)
        return False
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix="." + path.name)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return True


def render(template: str, values: dict[str, object]) -> str:
    """Substitute @TOKEN@ placeholders; ``%`` is doubled for systemd specifiers."""
    def substitute(match: re.Match[str]) -> str:
        try:
            value = values[match[1]]
        except KeyError:
            raise ValueError(f"no value for template token @{match[1]}@") from None
        return str(value).replace("%", "%%")

    rendered = re.sub(r"@([A-Z_]+)@", substitute, template)
    left = UNREPLACED.search(rendered)
    if left:
        raise ValueError(f"template token {left.group(0)} was not replaced")
    return rendered


def unit_path(target: Path, service_name: str) -> Path:
    return target / ".config/systemd/user" / service_name


def probe_path(target: Path) -> Path:
    return target / ".AItelier/bin/cpu-launcher-preexec.py"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, default=Path.home(),
                        help="HOME-shaped root to install into (default: this user's home)")
    parser.add_argument("--repo-root", type=Path, default=CHECKOUT,
                        help="checkout the rendered unit runs from (default: this script's checkout)")
    parser.add_argument("--python", type=Path,
                        help="interpreter for the launcher (default: <repo-root>/.venv/bin/python)")
    parser.add_argument("--socket", type=Path,
                        help="launcher socket path (default: <target>/.AItelier/cpu-launcher.sock)")
    parser.add_argument("--config", type=Path, required=True,
                        help="operator host-launcher config JSON (see docs/repository-host-launcher.md)")
    parser.add_argument("--service-name", default=SERVICE_NAME)
    parser.add_argument("--systemctl", default="systemctl")
    parser.add_argument("--enable", action="store_true",
                        help="after writing the files: daemon-reload, enable, then start the unit")
    args = parser.parse_args()

    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.service", args.service_name):
        parser.error("--service-name must be a plain .service name")
    # abspath, not resolve: the venv interpreter IS a symlink chain to the real
    # binary, and the unit must start the venv's python, not its target.
    target = Path(os.path.abspath(args.target.expanduser()))
    repo = Path(os.path.abspath(args.repo_root.expanduser()))
    values = {
        "REPO": repo,
        "PYTHON": Path(os.path.abspath((args.python or repo / ".venv/bin/python").expanduser())),
        "SOCKET": Path(os.path.abspath((args.socket or target / ".AItelier/cpu-launcher.sock")
                                       .expanduser())),
        "CONFIG": Path(os.path.abspath(args.config.expanduser())),
        "PREEXEC": probe_path(target),
    }
    template = TEMPLATE.read_text()
    probe_source = PROBE.read_text()
    try:
        rendered = render(template, values)
    except ValueError as error:
        sys.exit(f"host CPU launcher installation failed: {error}")

    unit = unit_path(target, args.service_name)
    probe = probe_path(target)
    unit_changed = atomic_text(unit, rendered, 0o644)
    probe_changed = atomic_text(probe, probe_source, 0o755)
    print(("Wrote " if unit_changed else "Unchanged ") + str(unit))
    print(("Wrote " if probe_changed else "Unchanged ") + str(probe))
    print(f"Socket: {values['SOCKET']}")
    print(f"Config: {values['CONFIG']}")
    if not args.enable:
        print("No service action: rerun with --enable to daemon-reload, enable and start "
              + args.service_name)
        return
    for step in (("daemon-reload",), ("enable", args.service_name), ("start", args.service_name)):
        subprocess.run([args.systemctl, "--user", *step], check=True)
        print("systemctl --user " + " ".join(step))
    print(f"Enabled and started {args.service_name}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        sys.exit("host CPU launcher installation failed: " + str(error))
