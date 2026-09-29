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

A whole-directory swap fixes both of those and was the first answer here. It is
still not enough, and the reason is worth stating because it is not obvious: a
rename gives a reader who OPENS the directory a complete set, but it does not
give a SEQUENCE of path-based reads a snapshot. skillflow's resolver reads each
context source by path, one at a time, so an ordered interleaving still breaks
it — readiness accepts generation A, the first input is read from A, a
replacement lands, the second input comes from B. Checking the gate between the
renames cannot see that, because the reader has already passed the gate.

So published seed content is IMMUTABLE for a ``(project_id, config_name)``:

* first publication stages into a UNIQUE ``<config>/_seed.<uuid>.tmp/`` —
  invisible to readers, because ``ContextResolver._resolve_cross_config`` skips
  any child of the config directory whose name ends in ``.tmp`` — writes
  ``.seed_ready.json`` last, and moves it into place with a SINGLE
  ``os.rename``. One atomic syscall: there is no window in which ``_seed``
  exists and is incomplete, and no window in which it is missing;
* re-publishing byte-identical content is idempotent and touches nothing, so a
  retried launch is safe;
* re-publishing DIFFERENT content is refused (``SeedAlreadyPublished``). A new
  seed needs a new project id, which is what the supported launch flow already
  does — ``run_pipeline(against_project=…)`` mints one per launch.

Immutability is what makes a multi-read reader safe, and it does it by
construction rather than by a lock spanning readiness and the whole context read.
The unique staging directory is what makes two concurrent publishers safe: they
cannot collide through a shared path, and the loser of the final rename is told
so rather than silently overwriting.

(The resolver's scan is one level deep, which is why the seed files must sit
directly at ``_seed/<name>`` and why a ``_seed/<generation>/`` layout would not
work: the resolver would never find them.)

LEGACY SUPPORT IS EXPLICIT, NEVER IMPLICIT. A ``_seed/`` written before this
change carries no marker and is therefore not published; the gate says so and
names the remedy. ``adopt_legacy_seed`` writes ONLY the marker, atomically, into
the directory that is already there — it moves no content and swaps no
directory, so there is no interval in which a pre-upgrade run reading that path
could see anything change. The alternative — a fallback that trusts a lone
non-empty seed file — is exactly the defect above: it cannot tell a legacy
directory from a half-written one, and its failure mode is starting an agent on
a partial seed.
"""

import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

MARKER = ".seed_ready.json"
STAGING_SUFFIX = ".tmp"          # skillflow's context resolver skips *.tmp


class SeedAlreadyPublished(Exception):
    """This (project, config) already has published seed content.

    Published seed content is immutable, so a launch that would replace it is
    refused rather than silently changing what a live reader resolves. The
    supported move is a new project id.
    """


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


def _published_files(directory: Path) -> dict[str, str] | None:
    """The content of the published generation, or None if none is published."""
    marker = directory / MARKER
    if not marker.is_file():
        return None
    try:
        names = json.loads(marker.read_text(encoding="utf-8")).get("files", [])
        return {n: (directory / n).read_text(encoding="utf-8") for n in names}
    except (OSError, ValueError):
        return None


def publish_seeds(directory: Path, files: dict[str, str], *,
                  review_input_bundle: dict | None = None) -> str:
    """Publish *files* as the seed of this (project, config). Returns its id.

    IMMUTABLE. Publishing the same content again is a no-op that returns the
    existing generation; publishing different content raises
    ``SeedAlreadyPublished``. Nothing already published is ever mutated, which is
    what makes a reader that resolves several sources one path at a time safe
    without any lock spanning its reads.
    """
    directory = Path(directory)

    existing = _published_files(directory)
    if existing is not None:
        stored_bundle = json.loads((directory / MARKER).read_text()).get("review_input_bundle")
        if existing == files and stored_bundle == review_input_bundle:
            return published_generation(directory) or ""
        raise SeedAlreadyPublished(
            f"{directory} already publishes {sorted(existing)}; refusing to "
            f"replace it with {sorted(files)}. Published seed content is "
            f"immutable — a live run resolves its context from these paths one "
            f"at a time, so replacing them can hand one run two different "
            f"generations. Launch the new content under a new project id.")
    if directory.exists() and any(p.name != MARKER for p in directory.iterdir()):
        raise SeedAlreadyPublished(
            f"{directory} holds unpublished content ({sorted(p.name for p in directory.iterdir())}). "
            f"If it was written before generation publishing, migrate it with "
            f"core.seed_publication.adopt_legacy_seed() while nothing is reading "
            f"it; otherwise launch under a new project id.")

    staging = directory.with_name(
        f"{directory.name}.{uuid.uuid4().hex}{STAGING_SUFFIX}")
    staging.mkdir(parents=True)
    for name, content in files.items():
        _atomic_write(staging / name, content)
    if review_input_bundle:
        from core.review_input_bundle import PREFIX
        for item in review_input_bundle["items"]:
            (staging / (PREFIX + item["name"])).chmod(0o444)
    generation = uuid.uuid4().hex
    _atomic_write(staging / MARKER, json.dumps(
        {"generation": generation, "files": sorted(files), "count": len(files),
         **({"review_input_bundle": review_input_bundle} if review_input_bundle else {})},
        ensure_ascii=False, indent=1))

    if directory.exists():
        # Empty (a bare directory some earlier code created): remove it so the
        # rename can land. Non-empty was refused above.
        try:
            directory.rmdir()
        except OSError:
            pass
    try:
        # ONE syscall. `_seed` goes from absent to a complete generation with no
        # observable state in between.
        os.rename(staging, directory)
    except OSError:
        # A concurrent publisher won the race. Unique staging means we have
        # damaged nothing; decide the same way a sequential caller would.
        shutil.rmtree(staging, ignore_errors=True)
        winner = _published_files(directory)
        winner_bundle = (json.loads((directory / MARKER).read_text()).get("review_input_bundle")
                         if winner is not None else None)
        if winner is not None and winner == files and winner_bundle == review_input_bundle:
            return published_generation(directory) or ""
        raise SeedAlreadyPublished(
            f"{directory} was published concurrently with different content; "
            f"launch under a new project id.")
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
    """Mark a pre-generation ``_seed/`` as published, in place.

    A deliberate migration, run once against a named workspace and only while
    nothing is reading it — never from the read path, because at read time a lone
    non-empty seed file is indistinguishable from a publication in progress.

    Writes ONLY the marker, atomically. No file is moved, rewritten or removed,
    so a pre-upgrade run resolving these paths sees exactly what it saw before;
    the single change is that the gate now accepts them. Returns the new
    generation id, or None if there was nothing to adopt.
    """
    directory = Path(directory)
    if (directory / MARKER).is_file():
        return None                                   # already a generation
    if not directory.is_dir():
        return None
    names = sorted(p.name for p in directory.iterdir()
                   if p.is_file() and not p.name.startswith("."))
    if not names:
        return None
    if not (directory / seed_file).is_file() or not (
            directory / seed_file).read_text(encoding="utf-8").strip():
        return None
    generation = uuid.uuid4().hex
    _atomic_write(directory / MARKER, json.dumps(
        {"generation": generation, "files": names, "count": len(names),
         "adopted": True},
        ensure_ascii=False, indent=1))
    return generation


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


def review_seed_context(directory: Path, config_name: str) -> dict:
    """Carry seed-bound admission metadata into runs created by the poller."""
    marker = Path(directory) / MARKER
    if not marker.exists():
        return {}
    identity = json.loads(marker.read_text()).get("review_input_bundle")
    if identity is None:
        return {}
    from core.review_input_bundle import manifest
    arguments = {"producer": identity.get("producer"), "items": [
        dict(item, content_base64=None) for item in identity.get("items", [])]}
    if manifest(arguments) != identity:
        raise ValueError("invalid published review input identity")
    return {"_review_input_bundle": identity, "_review_input_config": config_name}
