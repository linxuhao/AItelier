# `design/` — the game's durable design record

This directory is the game's **design record**. It outlives any single pipeline
run and it is the one place the game's rules and content are written down.

- `step2_design.md` is *per-run and disposable* — it describes one run's change
  program (which files get touched, which components get built).
- **`design/` is cumulative** — it describes what the game *is*.

## Who reads and writes it

| | |
|---|---|
| **Architect (step 2)** | reads it before designing. Its design must be consistent with this record, or it must declare a "设计变更" section saying what changes and why. |
| **PM (step 3)** | reads it. Every number in a task card and every playtest assertion threshold comes from `20_content.md`. |
| **`5_design` (after final verification passes)** | edits the files this run actually changed, and appends one line to `99_changelog.md`. |

`5_design` runs only after `5_review` passes, so this record describes the game
that **shipped**, never the one that was merely planned.

## Layout

| File | Holds |
|---|---|
| `00_overview.md` | pitch, player fantasy, what one session looks like, win/lose |
| `10_systems.md` | the decided rules — turn structure, movement, resolution, resources, progression, AI |
| `20_content.md` | roster / skills / levels / items, as tables with the real numbers |
| `30_presentation.md` | resolution & stretch, art style, audio, UI layout, input map |
| `90_decisions.md` | rejected ideas + why · open questions |
| `99_changelog.md` | append-only, one entry per run that changed the design |

**Split as it grows.** When a file passes ~300 lines, split it by topic keeping
the numeric prefix — `10_systems_combat.md`, `10_systems_progression.md`,
`20_content_roster.md`, `20_content_skills.md`. Filename order is the reading
order; there is no other index to keep in sync.

## Rules

1. **Edit surgically.** Change the lines that changed. Never regenerate a file
   wholesale — that is how three runs' worth of decisions disappear in one turn.
2. **`99_changelog.md` is append-only.** Never rewrite or delete an entry.
3. **Only what is implemented** belongs in `10_`/`20_`/`30_`. Planned-but-absent
   goes to `90_decisions.md` under open questions.
4. **Numbers here are the source of truth** for implementers. Vague here becomes
   invented in the code.

It is committed to git — if a run ever guts a file, `git log -p design/` has
every prior version.
