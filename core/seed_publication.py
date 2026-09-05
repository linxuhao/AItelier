"""Publishing a run's seed, and asking whether it has been published.

A project row is a SCHEDULING TRIGGER. The poller selects on
``(status='planning', config_name IN <scheduler-owned>)``, so from the instant
``ensure_project`` inserts the row the poller may create and start a run for it —
and it did, live on 2026-09-05, one second after ``POST /api/projects`` registered
a reserved worktree with ``config_name=coding_impl``. The spawned implementer had
no ``plan.md``, so it had no scope, no goals and no restrictions; it adopted a
prior round's ``DIAGNOSIS.md`` as "the plan" and edited it.

Two separate things are needed, and an existence check is only one of them.

**Publication must be atomic.** ``seed_ready`` must never be true over a
half-written file, and a config with several seed inputs must not be visible
after the first of them lands. Files are written to a temp name and ``os.replace``
d into place (atomic within a directory on POSIX), and the READY marker is
written last, the same way. The marker is therefore the single publication point
for the whole set: the poller reads one file and learns about all of them.

**The gate must be narrow enough to be true.** It applies only to a config whose
GRAPH declares a same-config context source naming the manifest's ``seed_file``
— that is, a config that actually reads its own seed. That is derived from the
graph, never a hardcoded list, and it matters:

  * ``dpe_default_v2`` declares ``seed_file: project_brief.md`` but its steps
    never read ``{config: dpe_default_v2, output: project_brief.md}`` — the brief
    reaches step 1 as a cross-config import from ``meta_conversation``, guarded
    already by ``missing_cross_config_inputs`` and the ``meta_state='drafting'``
    flag. A blanket "seed_file must exist" gate would have stalled every DPE
    build in the system, which is exactly the kind of collateral a guard is not
    allowed to have.
  * A same-config STEP output is not a seed and cannot be mistaken for one here:
    a seed source is ``{config: <self>, output: <seed_file>}`` with no ``step``
    key, while ``coding_impl``'s feedback input is ``{step: "test"}`` — no
    ``config``, and its ``output`` is not the seed file. Both halves are checked.
"""

import json
import os
import tempfile
from pathlib import Path

MARKER = ".seed_ready.json"


def seed_dir(sf, project_id: str, config_name: str) -> Path:
    return sf._workspace.get_config_path(project_id, config_name) / "_seed"


def _atomic_write(path: Path, text: str) -> None:
    """Write so that the file never exists in a partial state.

    A reader that sees the name sees the whole content: the data is written to a
    temp file in the SAME directory (so ``os.replace`` stays within one
    filesystem and is therefore atomic) and renamed over the target.
    """
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-",
                               suffix=path.name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def publish_seeds(directory: Path, files: dict[str, str]) -> Path:
    """Write every seed file, then publish the set. Returns the marker path.

    Ordering is the contract: the marker is written LAST and atomically, so
    ``seed_is_published`` is false for the entire time the set is incomplete and
    true only once every file is whole and present.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        _atomic_write(directory / name, content)
    marker = directory / MARKER
    _atomic_write(marker, json.dumps(
        {"files": sorted(files), "count": len(files)},
        ensure_ascii=False, indent=1))
    return marker


def seed_is_published(directory: Path, seed_file: str) -> tuple[bool, str]:
    """Is this config's seed set fully published? Returns (ok, reason).

    ``reason`` is for the tick log, which is the only place a waiting project is
    visible at all.
    """
    marker = directory / MARKER
    if marker.is_file():
        try:
            names = json.loads(marker.read_text(encoding="utf-8")).get("files", [])
        except (OSError, ValueError) as e:
            return False, f"{MARKER} unreadable ({type(e).__name__})"
        for name in names:
            f = directory / name
            if not f.is_file():
                return False, f"published seed '{name}' is missing"
            if not f.read_text(encoding="utf-8").strip():
                return False, f"published seed '{name}' is empty"
        if seed_file and seed_file not in names:
            return False, f"seed set does not include '{seed_file}'"
        return True, "published"

    # Compatibility, for a seed written before this module existed: no marker,
    # but the declared seed file is there and has content. Deliberately weaker
    # than the marker path — it can only speak for the ONE file it can name —
    # and it is reachable only for pre-existing workspaces, because every launch
    # now publishes a marker. Without it, a project seeded by the old code whose
    # run had not been created yet would stall for good on the upgrade.
    f = directory / seed_file if seed_file else None
    if f is not None and f.is_file() and f.read_text(encoding="utf-8").strip():
        return True, f"no {MARKER}; '{seed_file}' present (pre-publication seed)"
    if f is not None and f.is_file():
        return False, f"'{seed_file}' is empty"
    return False, f"'{seed_file}' has not been written"


def reads_own_seed(graph, config_name: str, seed_file: str) -> bool:
    """Does this graph actually read ``{config: <self>, output: <seed_file>}``?

    The narrowing described in the module docstring. ``step`` must be absent:
    with a step it names that step's OUTPUT, which the run produces for itself
    and no launcher could ever pre-write.
    """
    if not seed_file:
        return False
    for node in getattr(graph, "steps", []) or []:
        for spec in (node.context or []):
            source = spec.get("source", spec)
            if not isinstance(source, dict):
                continue
            if (source.get("config") == config_name
                    and not source.get("step")
                    and source.get("output") == seed_file):
                return True
    return False
