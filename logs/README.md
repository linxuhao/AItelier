Focused-probe logs for notes-public rev 4. Each .txt holds the exact command, the
worktree cwd and interpreter, the recorded result line, and the bare exit code taken
from the probe's own `exit_status` — never through a pipe. Files end in .txt on
purpose: the repository .gitignore is *.log, so a .log output file is Git-ignored and
the implement lifecycle hook refuses the whole step.

The delivery note at ../final/delivery-note.md cites these by name next to every number:
rev4_criteria_named_ids.txt  - every test id named in the note, run, with its bare RC.
rev4_full_suite_probe.txt    - the whole-suite focused_check (hit the 300s wall, not a
                               pass here) and the order-dependent test run alone.
rev4_doc_edits_readback.txt  - each doc/test edit read back from the file; the note's
                               "改后" text is copied from these read-backs.
