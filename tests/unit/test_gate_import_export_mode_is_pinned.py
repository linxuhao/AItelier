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
