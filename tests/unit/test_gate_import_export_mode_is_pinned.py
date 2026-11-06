"""The image-owned gate client must be readable by the user the backend runs as.

Every authored repository gate imports /opt/aitelier-gate-imports/docker/godot/
godot_harness.py before it can measure anything. A plain COPY preserves the BUILD
HOST's file mode, and this host's working tree carries that file as 0600 (umask
077; git says 100644, so only a fresh checkout would bake 0644). The image baked
0600 root:root while the backend runs as uid 1000, so the import raised
PermissionError - measured in the live container:

    $ docker exec -u 1000 aitelier head -c1 /opt/.../godot_harness.py
    Permission denied

That is invisible until a gate runs, and a gate that cannot run looks like a gate
that failed. This test reads the Dockerfile rather than the image so it can fail
at review time, before a rebuild is spent.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXPORT = "/opt/aitelier-gate-imports"
TARGET = "/opt/aitelier-gate-imports/docker/godot/godot_harness.py"


def test_the_baked_gate_client_pins_its_mode():
    text = (ROOT / "Dockerfile").read_text()
    copies = [ln for ln in text.splitlines()
              if ln.strip().upper().startswith("COPY") and TARGET in ln]
    assert copies, f"no COPY bakes {TARGET}; the gate client must come from the image"
    for line in copies:
        assert re.search(r"--chmod=0?6?4?4|--chmod=0[0-7]{3}", line), (
            f"this COPY inherits the build host's mode and can bake 0600 root:root:\n  {line}\n"
            "Pin it with --chmod=0644: the backend runs as uid 1000 and cannot read 0600.")


def test_no_other_opt_copy_of_repository_source_is_left_unpinned():
    """Same class of bug: an image-owned copy of repository source read by a
    non-root runtime must not inherit the host's mode."""
    text = (ROOT / "Dockerfile").read_text()
    offenders = [ln for ln in text.splitlines()
                 if ln.strip().upper().startswith("COPY") and "/opt/" in ln
                 and "--chmod" not in ln and "--from=" not in ln]
    assert not offenders, (
        "these COPY lines bake repository source outside /app without pinning a mode:\n  "
        + "\n  ".join(offenders))


def _run_commands(text: str) -> list[str]:
    """Every RUN command, with its `\\` continuations joined into one line."""
    commands: list[str] = []
    current: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if current is None:
            if stripped.upper().startswith("RUN "):
                current = stripped[4:]
            else:
                continue
        else:
            current = current + " " + stripped
        if current.endswith("\\"):
            current = current[:-1].rstrip() + " "
            continue
        commands.append(" ".join(current.split()))
        current = None
    if current is not None:
        commands.append(" ".join(current.split()))
    return commands


def test_the_import_tree_is_searchable_and_not_only_the_file():
    """A pinned FILE inside an untraversable DIRECTORY is still unreadable.

    `COPY --chmod=0644 <file> /opt/.../godot/<file>` also applies 0644 to the
    parent directories BuildKit creates for it. Measured on an image built from
    82bb9718f7 (2026-11-06, image e177dafdb87f): /opt/aitelier-gate-imports,
    /opt/aitelier-gate-imports/docker and .../docker/godot all came out
    0644 root:root, so as uid 1000 `ls` answered "Permission denied" and
    `import docker.godot.godot_harness` was a ModuleNotFoundError - a worse
    failure than the 0600 file the neighbouring test fixed, while that test
    stayed green because the Dockerfile does say `--chmod=0644`.

    This file reads the Dockerfile because the alternative is finding out from
    a deployed gate. What it can assert is the normalisation step; the modes
    actually baked can only be measured inside a built image, and that
    measurement belongs to the deployment that builds it. Accepts either shape:
    a `chmod 0755 <dirs>` (or `install -d -m 0755 <dirs>`) naming all three
    directories, plus the package markers pinned readable.
    """
    commands = _run_commands((ROOT / "Dockerfile").read_text())
    directories = (EXPORT, f"{EXPORT}/docker", f"{EXPORT}/docker/godot")
    for directory in directories:
        assert any(directory in command.split() and "0755" in command
                   for command in commands), (
            f"nothing normalises {directory} to 0755: a 0644 directory cannot be "
            "traversed, so the gate client inside it is unreadable even though the "
            "file itself is 0644.\nRUN commands seen:\n  " + "\n  ".join(commands))
    for marker in (f"{EXPORT}/docker/__init__.py", f"{EXPORT}/docker/godot/__init__.py"):
        assert any(marker in command.split() and "0644" in command
                   for command in commands), (
            f"{marker} is created by `touch` and inherits the build umask; pin it 0644.")

