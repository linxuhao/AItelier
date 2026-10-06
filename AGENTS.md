# Director assignment — where it lives

Who directs 武虾传奇 (or any project), which agent implements what, and what may be started or
published is NOT recorded here. It lives in the State DAG: attempts, node holds, issues and
priorities for what is in flight, the project's driver note (`get_driver_note`) for standing
rules, and node acceptance criteria for what counts as done. An earlier edition of this section
named the director and the game checkout by hand and went stale; this one names a location.

- One writer per checkout and one controller per run. No `commit -a`, broad kills or shared GPU changes; force push and public releases need the owner's explicit authorization.
- Production pipeline checkpoints=ask; use `debugctl.py await PROJECT --run RUN --follow`, no polling.
- Run AItelier pytest only in a throwaway container (`docker run --rm ... aitelier:latest`, network none, 2 CPU / 2 GiB, one worker, at most 4 at once), never inside the production `aitelier` container — on 2026-09-23 a batch of suites there OOM-killed uvicorn. `docker exec aitelier` is only for git worktree add/remove, rev-parse, status and py_compile.
- Independent tests must validate the final commit. Zero-assertion/skipped/blind gates do not pass; first failure evidence stays visible.
- Commercial source and full content remain private. Only reviewed public/ manifests may publish; no complete PCK or copied design/. Old publicly released history cannot be assumed withdrawn.
- Verify and print current timestamp before user-facing responses. Keep the State DAG current on assignment/queue/run changes.
- State DAG nodes carry a facet and the engine refuses edges that build on an implementation instead of a contract (2026-09-09): read `design/state_facets.md` and the "Facets" section of the driver guide (`state_graph_help`) before adding or re-pointing nodes; `facet_lint` shows every violation.

The repository documentation below describes AItelier itself.

---

