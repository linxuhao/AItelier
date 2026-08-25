# Install route: every step, and what can go wrong at it

The path a new user walks from an empty machine to a finished pipeline, with the
failure modes actually reproduced at each step and the doc or skill that covers
them. Anything marked **GAP** is not covered anywhere yet.

Reproduced on 2026-08-25, not reasoned about: first against a clean copy of
`docker-compose.yml` in an empty directory, then **end-to-end on a second machine**
(`linxuhao-ai`) — public clone, fresh venv, `docker build --no-cache`, virgin
`HOME`, container up and answering. Numbers from that run are inline below.

```
  1. clone + pip install -e .
       ↓
  2. get a model key                    ← which key? provider-agnostic
       ↓
  3. aitelier   (starts Docker)         ← two hard stops used to live here
       ↓
  4. describe what to build             ← meta_conversation
       ↓
  5. approve checkpoints                ← DPE runs; the gates report
       ↓
  6. read the result / fix              ← run page, trace
       ↓
  (optional) generate your own pipeline ← forge, then DRIVE it
  (optional) drive it from DSH          ← the plugin + its skill
```

---

## 1. Clone and install

| Can go wrong | Covered by |
|---|---|
| Python < 3.12 (macOS system `python3` is usually older) | README **Install** states 3.12+ and the check |
| `pip install -e .` pulls `skillflow-py` from PyPI — it is a hard dependency, not vendored | README says so |
| The clone itself needing credentials | **Verified**: anonymous `git clone` of the public repo works with `GIT_TERMINAL_PROMPT=0` and no helper |
| PyPI serving a stale skillflow | **Verified**: a fresh venv resolved `skillflow-py 1.5.42` |

## 2. Get a model key

| Can go wrong | Covered by |
|---|---|
| **Getting the wrong provider's key.** AItelier is provider-agnostic; the key you need is whatever the shipped `agent_configs` reference. This went stale once — the configs moved to `ark/` while the README still said DeepSeek, which would fail every step of a new user's first run | README **Quick Start** names the right key and shows the `grep` to confirm; `test_the_readme_names_a_key_the_shipped_configs_actually_use` fails if they diverge again |
| **Putting the key in `.env`.** Keys are secret FILES; `.env` is for endpoints. The two docs used to contradict each other on this exact step | README + `.env.example` now agree; a test pins that the README does not say `.env` |
| Key missing at run time | The failure names the provider, the key, and the file to create — `core/external_deps.py` |
| **The CLI naming a different key than the README.** `_LLM_SECRET` hard-coded `DEEPSEEK_API_KEY`, so a cold install was told to create one key by the docs and another by the tool | Fixed: derived from the shipped configs via `required_llm_keys()`; a test asserts the two agree |

## 3. `aitelier` (Docker starts)

Both of these were **hard stops before any container ran**, each naming a
resource but not what it was for or that it was optional. Both are fixed; listed
because the errors are still what you would see if you bypass the CLI.

| Can go wrong | Covered by |
|---|---|
| `network vip-gateway_default declared as external, but could not be found` — a compose file that declares an external network refuses to run when it is absent | Moved to the opt-in `docker-compose.edge.yml`; `AITELIER_EDGE_NETWORK` turns it on. Header comment explains why |
| `invalid mount config … /.aitelier-secrets/GITHUB_TOKEN` — Docker refuses a missing secret SOURCE; an empty file is the correct content for "I don't use this", which the error never says. Four in a row | `cli/server.py:_ensure_host_dirs` creates them; README gives the one-liner for a hand-run `docker compose` |
| Docker not running at all | `_require_docker` raises; there is **no host-process fallback** by design, and the README now says so where the user first meets it |
| Port 4444 already taken by a stale non-Docker server | The CLI kills it before starting |
| **The container starting and then crash-looping on `sqlite3.OperationalError: unable to open database file`.** Docker creates a missing bind-mount source ITSELF, as root; the container runs as the host uid and cannot write it. Hits every machine where `~/.AItelier` does not already exist — i.e. every new one, which is why no developer sees it | Fixed: `_ensure_host_dirs` creates the state root first; a test reads the `${HOME}` mounts out of compose so a new one cannot be forgotten |
| First `docker build` being slow or broken | **Verified**: `--no-cache` build succeeded in **52s**, 1.19GB |

## 4. Describe what to build

| Can go wrong | Covered by |
|---|---|
| Starting `dpe_default_v2` directly, skipping the meta conversation. It needs `step1_goals.json` from `meta_conversation/finalize` | The launcher refuses with a message naming the file, the config that produces it, and what to run instead |
| Nothing appears to happen | `~/.AItelier/logs/scheduler_ticks.log` — one line per tick with a stable outcome token. README shows the greps |

## 5. Approve checkpoints, pipeline runs

| Can go wrong | Covered by |
|---|---|
| `web_search` unavailable | Refuses naming `SEARXNG_URL`; agents fall back to model knowledge |
| Godot gates unavailable (game projects) | **Skips loudly**: `gate_skipped: true`, the reviewer is told the code shipped UNVERIFIED, and the skip is recorded in `~/.AItelier/logs/gate_skips.log`. It does not silently pass |
| Media generation unavailable | Refuses naming `AITELIER_MEDIA_MCP_URL` — and the doc warns that server holds the **cast**, so repointing it mid-project recasts every character |
| One project starving the others | Fixed: the scheduler now runs different projects in parallel, serial within a project (`AITELIER_MAX_CONCURRENT_PROJECTS`, default 4) |
| **GAP — a long step makes the tick log go quiet.** A tick that is executing logs nothing until it returns, so "no lines for eight minutes" and "the scheduler is wedged" look identical. The log exists precisely to make stalls readable | not covered |

## 6. Read the result

| Can go wrong | Covered by |
|---|---|
| Not knowing where to look after a failure | Run page shows the graph with each node's state; the skill's outside-in table (`get_run_summary` → `trace_list` → `trace_read` → `get_step_output`) |
| **A step that routed to a failure gate did not "fail"** — `first_failure` is null and the run error is `Node 'input_failed' reached` | The DSH skill calls this out; the web UI does not |
| A fan-out's steps look identical | `loop_item` (skillflow ≥1.5.41): the run graph groups the loop body in its own box with an item picker, and `get_run_summary` names the item |
| **GAP — an old run predates `loop_item`.** The UI says "not recorded" honestly, but there is no way to recover the attribution | not recoverable by design; the information was never written |

## 7. Generate your own pipeline

| Can go wrong | Covered by |
|---|---|
| Believing a green generation. Three structural gates prove SHAPE, not behaviour — one pipeline needed **four drives**, each exposing the next layer | The DSH skill leads with it; `forge_palette` teaches the rules |
| The seed is never wired to the first step | Taught rule `the_seed_actually_reaches_the_first_step` (cannot be enforced — not every pipeline takes input) |
| `validation:` written as a mapping instead of a list of specs | Enforced gate `validation_is_a_spec_list` |
| A generated tool falls back instead of failing, reporting an accurate error about a question nobody asked | The skill names the shape; no gate can catch it |
| **GAP — role quality.** A structurally perfect pipeline whose reviewer writes an empty file or a mis-shaped verdict. Only a drive finds it, and only reading the trace explains it | the skill points at `trace_read`; nothing prevents it |

## 8. Drive it from DeepSeek Harness

| Can go wrong | Covered by |
|---|---|
| **No `mcp__aitelier__*` tools and no error.** `dsh-mcp-client` has `failOnStartupError: false`, so an unreachable endpoint does not stop `dsh` booting — the tools simply never appear. An agent in that state cannot diagnose it from the inside | The plugin README's first section, a four-step checklist |
| Wrong URL for where `dsh` runs (host vs container) | Same checklist |
| `wait_for_run` outliving the client's own timeout — the client hangs up first and the model sees a transport error rather than "still running" | The patch raises `toolCallTimeoutMs` to 10 minutes, above `wait_for_run`'s default |
| Write tools all answer `denied:` | Expected without `AITELIER_ADMIN_TOKEN` — a legitimate read-only install, and the README says so |
| The skill is not installed | It ships in the package but is **not auto-mounted**: a Cordis patch replaces a whole row, so wiring it would clobber the user's own skill roots. README gives the `cp` |
| **The `cp` pointing at the wrong tree.** `dsh plugin add` installs into `$DSH_HOME/profiles/<name>/node_modules`, not the project's — the first version of the command read a bare `node_modules/…` and found nothing from a project directory | Fixed and **verified** by installing both plugins into an isolated `DSH_HOME`; a test pins that the command copies from `profiles/` into `~/.dsh/skills` |
| Two plugins colliding | **Verified**: `dsh-plugin-aitelier` + `dsh-plugin-continuity` compose side by side — distinct row ids (`mcp-aitelier` / `continuity`) and distinct `serverName`s, so the model sees `mcp__aitelier__*` and `mcp__continuity__*` |

---

## What this map is missing

The cold-machine rehearsal is **done** — and it paid for itself twice, finding
the root-owned state directory and the CLI/README key disagreement, neither of
which any amount of reading would have surfaced. What is still open:

- **The second machine was not a stranger.** `linxuhao-ai` is the author's other
  box: Docker was installed and working, Python 3.12 was present, and the whole
  run used an overridden `HOME` rather than a fresh account. A machine without
  Docker, or on macOS, or behind a proxy, is still untested.
- **Nothing past a healthy container was exercised ON THE COLD MACHINE.** That
  install answered `/health` and registered 13 pipelines but never ran a step,
  because a step needs a real key. On the author's host the DSH end is now
  verified for real: `dsh --profile headless` on a hand-declared Ark route
  (`provider: ark`, `deepseek-v4-flash`) called `mcp__aitelier__list_pipelines`
  and answered correctly, then diagnosed a genuinely failed run — following the
  skill's outside-in path (`list_runs` → `get_run_summary` → `trace_list`),
  quoting the tool's own error out of the trace, and calling
  `Node 'input_failed' reached` **"a symptom, not the cause"**, which is the one
  trap the skill exists to teach. What remains unrun is a pipeline EXECUTION
  driven from DSH end to end.
- **Nothing verifies the docs against a run.** The tests pin the README against
  the *configs* (which key, which default model), not against a completed
  install. A doc can be self-consistent and still describe a path nobody can
  walk.
