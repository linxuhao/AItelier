"""Publishing a run's seed as a whole generation, and asking whether one exists.

A project row is a SCHEDULING TRIGGER. The poller selects on
``(status='planning', config_name IN <scheduler-owned>)``, so from the instant
``ensure_project`` inserts the row the poller may create and start a run for it —
and it did, live on 2026-09-05, one second after ``POST /api/projects`` registered
a reserved worktree with ``config_name=coding_impl``. The spawned implementer had
no ``plan.md``, so it had no scope, no goals and no restrictions; it adopted a
prior round's ``DIAGNOSIS.md`` as "the plan" and edited it.

The FIRST repair filled ``_seed/`` in place — atomic per file, marker written
last — and an independent reviewer showed that is not enough, twice over:

* on a FRESH publication the first ``os.replace`` makes ``plan.md`` non-empty, and
  the "legacy" fallback (no marker, seed file present) then answered *published*
  over a set that was still being written. Nothing distinguished a legacy
  directory from a publication in progress, because nothing could;
* on a REPLACEMENT the previous marker stayed visible while the new files landed
  underneath it, so a reader got ``plan=new`` with ``extra=old`` and a marker
  saying *published*. Per-file atomicity cannot publish a SET.

So publication swaps a whole directory instead of filling one:

1. stage into ``<config>/_seed.tmp/`` — invisible to readers, because skillflow's
   ``ContextResolver._resolve_cross_config`` skips any child of the config
   directory whose name ends in ``.tmp``. (That same scan is one level deep,
   which is why the seed files must sit directly at ``_seed/<name>`` and why a
   ``_seed/<generation>/`` layout would not work: the resolver would never find
   them.)
2. write ``.seed_ready.json`` last, inside the staging directory, carrying a fresh
   ``generation`` id and the file list;
3. ``rename(_seed -> _seed.old.tmp)``, then ``rename(_seed.tmp -> _seed)``, then
   delete the old one.

Between the two renames ``_seed`` does not exist, so the gate is FALSE and the
resolver finds nothing. That intermediate state is *not published* — neither
mixed nor partial — which is the safe answer, and a poller simply waits a tick.

LEGACY SUPPORT IS EXPLICIT, NEVER IMPLICIT. A ``_seed/`` written before this
change carries no marker and is therefore not published; the gate says so and
names the remedy. ``adopt_legacy_seed`` republishes such a directory as a proper
generation and is a deliberate migration step, never reachable from the read
path. The alternative — a fallback that trusts a lone non-empty seed file — is
exactly the defect above: it cannot tell a legacy directory from a half-written
one, and its failure mode is starting an agent on a partial seed.
"""

import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

MARKER = ".seed_ready.json"
STAGING_SUFFIX = ".tmp"          # skillflow's context resolver skips *.tmp
RETIRED_SUFFIX = ".old.tmp"


def seed_dir(sf, project_id: str, config_name: str) -> Path:
    return sf._workspace.get_config_path(project_id, config_name) / "_seed"


def _atomic_write(path: Path, text: str) -> None:
    """Write so that the file never exists in a partial state.

    A reader that sees the name sees the whole content: the data goes to a temp
    file in the SAME directory (so ``os.replace`` stays within one filesystem and
    is atomic) and is renamed over the target.

    This is per-FILE atomicity only. It is necessary and it is not sufficient —
    see the module docstring for why the set needs a directory swap on top.
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


def publish_seeds(directory: Path, files: dict[str, str]) -> str:
    """Publish *files* as one new generation of *directory*. Returns its id.

    Nothing in *directory* is touched until the whole generation is staged and
    marked; the visible switch is a rename.
    """
    directory = Path(directory)
    staging = directory.with_name(directory.name + STAGING_SUFFIX)
    retired = directory.with_name(directory.name + RETIRED_SUFFIX)

    # A previous run that died mid-swap leaves these behind. Clearing them here
    # rather than on the read path keeps the reader read-only.
    for leftover in (staging, retired):
        if leftover.exists():
            shutil.rmtree(leftover, ignore_errors=True)

    staging.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        _atomic_write(staging / name, content)
    generation = uuid.uuid4().hex
    _atomic_write(staging / MARKER, json.dumps(
        {"generation": generation, "files": sorted(files), "count": len(files)},
        ensure_ascii=False, indent=1))

    if directory.exists():
        os.rename(directory, retired)
    os.rename(staging, directory)
    shutil.rmtree(retired, ignore_errors=True)
    return generation


def published_generation(directory: Path) -> str | None:
    """The id of the generation currently published, or None.

    Two reads returning the same id saw the same set — which is how a test proves
    a reader never observed a mixture.
    """
    try:
        return json.loads(
            (Path(directory) / MARKER).read_text(encoding="utf-8")
        ).get("generation")
    except (OSError, ValueError):
        return None


def seed_is_published(directory: Path, seed_file: str) -> tuple[bool, str]:
    """Is a COMPLETE generation published here? Returns (ok, reason).

    ``reason`` is for the tick log, which is the only place a waiting project is
    visible at all. There is no fallback: a directory with no marker is not
    published, whatever it happens to contain.
    """
    directory = Path(directory)
    marker = directory / MARKER
    if not marker.is_file():
        if directory.exists():
            return False, (
                f"no {MARKER}: nothing published here. If this workspace was "
                f"seeded before generation publishing, migrate it explicitly "
                f"with core.seed_publication.adopt_legacy_seed()")
        return False, f"no seed directory: '{seed_file}' has not been published"
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        names = data.get("files", [])
    except (OSError, ValueError) as e:
        return False, f"{MARKER} unreadable ({type(e).__name__})"
    if seed_file and seed_file not in names:
        return False, f"published generation does not include '{seed_file}'"
    for name in names:
        f = directory / name
        if not f.is_file():
            return False, f"published seed '{name}' is missing"
        if not f.read_text(encoding="utf-8").strip():
            return False, f"published seed '{name}' is empty"
    return True, f"generation {data.get('generation', '?')[:8]} ({len(names)} file(s))"


def adopt_legacy_seed(directory: Path, seed_file: str) -> str | None:
    """Republish a pre-generation ``_seed/`` as a proper generation.

    A deliberate migration, run once against a named workspace — never from the
    read path, because at read time a lone non-empty seed file is
    indistinguishable from a publication in progress.

    Returns the new generation id, or None if there was nothing to adopt.
    """
    directory = Path(directory)
    if (directory / MARKER).is_file():
        return None                                   # already a generation
    if not directory.is_dir():
        return None
    files = {p.name: p.read_text(encoding="utf-8")
             for p in sorted(directory.iterdir())
             if p.is_file() and not p.name.startswith(".")}
    if not files or not files.get(seed_file, "").strip():
        return None
    return publish_seeds(directory, files)


def reads_own_seed(graph, config_name: str, seed_file: str) -> bool:
    """Does this graph actually read ``{config: <self>, output: <seed_file>}``?

    The narrowing that keeps the gate honest. ``step`` must be absent: with a
    step it names that step's OUTPUT, which the run produces for itself and no
    launcher could ever pre-write.
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
