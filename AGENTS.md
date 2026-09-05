# Director workflow override — 2026-09-05

The owner appointed Codex director of 武虾传奇 / wuxia-myth and authorized Claude/subagents to implement gameplay in isolated worktrees. This supersedes the previous game-specific pipeline-only rule. Codex owns scope and acceptance; AItelier is used for bounded, objectively verifiable batch work, not every change.

- Production is ssh linxuhao@linxuhaserver, game /home/linxuhao/.AItelier/projects/jinyong-assets. Read ~/.AItelier/DRIVER_STATE.md there before resuming. Read game AGENTS.md and design/01_process.md for current rules.
- One writer per checkout and one controller per run. No commit -a, broad kills, automatic public releases or shared GPU changes.
- Production pipeline checkpoints=ask; use debugctl.py await PROJECT --run RUN --follow, no polling. Legacy jinyong-clean/R6 is paused and superseded; do not resume its old brief.
- Claude may implement assigned gameplay fixes; director handles docs/tooling/integration. Independent tests must validate the final commit. Zero-assertion/skipped/blind gates do not pass; first failure evidence stays visible.
- Commercial source and full content remain private. Only reviewed public/ manifests may publish; no complete PCK or copied design/. Old publicly released history cannot be assumed withdrawn.
- Verify and print current timestamp before user-facing responses. Maintain the server driver state on assignment/queue/run changes.

The repository documentation below describes AItelier itself; this local checkout is not the production checkout.

---

