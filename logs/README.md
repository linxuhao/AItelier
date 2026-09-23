Focused-probe logs for notes-public rev 2 (third pass, relay 1). Each .txt holds the exact
command, the worktree cwd and interpreter, the pytest result line, and the bare exit code
taken from the probe's own recorded exit_status — never through a pipe. Files end in .txt
on purpose: the repository's .gitignore line 67 is *.log, so a .log output file is
Git-ignored and the implement lifecycle hook refuses the whole step. The delivery note at
../final/delivery-note.md cites these files by name next to every number:
criteria_rerun_5files.txt, criteria_rerun_8files.txt, criteria_named_nodes_13.txt,
state_surface_chunks.txt, full_suite_attempt.txt (the last one records what could NOT be
measured inside the probe's 300-second wall).
