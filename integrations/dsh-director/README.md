# dsh-director

A resident director and its native workers, as a plugin for DSH (DeepSeek Harness). The
director takes work, freezes each piece into a todo plan from a reviewed template, hands each
plan to a native DSH worker, checks what the worker delivered, and sleeps when nothing is left.

Where work comes from and where it is recorded belongs to a *host*. This README is about the
host built into the plugin, `hosts/local.mjs`: no server, no database, one DSH process and a
directory of plain files. (An external host replaces it; the repository's backend adapter in
`../dsh-driver` is one. Its install route is in the repository's top-level README.)

What the local host gives the director: work items in `work.json`, notes in `notes.md`, tasks and
answers dropped into `inbox/`, questions to you in `questions.json`, a progress log, todo plans and
native workers. What it does not have: a dependency graph, evidence or reviews, a dashboard, chat
channels (the Slack and WeChat channel plugins need a host that provides channels; next to this
host they do not load), or several directors coordinating.

## Prerequisites

- **Linux.** Tested on x86_64. On Windows the plugin refuses to start. macOS is untested: plans
  work there, but `watch_process` cannot see jobs (it reads `/proc`).
- **DSH `0.2.1-rc.5`** (the release this plugin is tested on) and the Node.js it needs (tested
  with Node 24). Another DSH release starts with a warning in the DSH log.
- **python3 with PyYAML**, for the todo package. On Debian and Ubuntu: `sudo apt install python3-yaml`
  (their python3 has no pip, and pip refuses to install into it). Elsewhere:
  `python3 -m pip install -r requirements.txt`, or in a venv
  (`python3 -m venv <dir> && <dir>/bin/pip install -r requirements.txt`, then set
  `todo: { python: <dir>/bin/python3 }` in the config below).
- **git**, for plans and their delivery.
- **A model key.** DSH's built-in DeepSeek route, provider `deepseek-official`, reads
  `DEEPSEEK_API_KEY` (from the environment or DSH's credential store).

At startup the local host checks python3, PyYAML and git and refuses to start, naming each
one that is missing.

## Use a profile of its own

**Never add this plugin to a profile you use for anything else.** It takes over every session
of its DSH process: the session named by `sessionId` is the director and every other session
is treated as a worker (fewer tools, no subagents, a worker's prompt). If the director cannot start, the
whole DSH process exits.

DSH's shipped profiles cannot run it either: in `web` the plugin waits forever for the
compaction service, in `headless` for the credentials service (the log says
`pending (waiting for service: ...)` and nothing else happens). Use a profile built on
`@deepseek-ai/dsh-base` alone, as below.

## 1. Install

```sh
npm install -g @deepseek-ai/dsh@0.2.1-rc.5
cd integrations/dsh-director            # in your checkout of this repository
sudo apt install python3-yaml           # Debian/Ubuntu; elsewhere: python3 -m pip install -r requirements.txt
```

## 2. Write the profile

Run this from `integrations/dsh-director`. `ROOT` is where the director keeps its files; pick
any directory. Paths must not contain spaces (they are written into YAML unquoted).

```sh
PLUGIN=$PWD
ROOT=$HOME/director
PROFILE=${DSH_HOME:-$HOME/.dsh}/profiles/director
mkdir -p "$PROFILE" "$ROOT/inbox"
cat > "$PROFILE/package.json" <<'EOF'
{ "private": true, "dsh": { "profile": { "bundles": ["@deepseek-ai/dsh-base"] } } }
EOF
cat > "$PROFILE/cordis.patch.yml" <<EOF
- id: hmr
  disabled: true
# Workers: DSH's native subagent tool, on the model the workers should use.
- id: tool-subagent
  config:
    provider: spawn
    toolName: subagent
    agentOptions: { provider: deepseek-official, model: deepseek-flash }
# Built-in tools the director's protocol does not use.
- { id: tool-subagent-fork, disabled: true }
- { id: tool-workflow, disabled: true }
- { id: plan-mode, disabled: true }
- { id: tool-todo, disabled: true }
- insert:
    - id: dsh-director
      name: $PLUGIN/index.mjs
    - id: director-local
      name: $PLUGIN/hosts/local.mjs
      config:
        root: $ROOT
        provider: deepseek-official
        model: deepseek-flash
EOF
```

`root`, `provider` and `model` are required. Optional keys of `director-local`'s `config`:

| key | default | what it does |
|---|---|---|
| `sessionId` | `director` | the director's DSH session; a restart resumes it. Give each director on one machine its own |
| `objective` | none | the director's standing goal (its DSH goal), set once on the first start |
| `permissionPreset` | none | a DSH permission preset for the director and its workers (below) |
| `dshEntry` | the running `dsh`, symlinks resolved | DSH's `lib/bin.js`; set it when DSH's libraries are not found from there |
| `todo` | `{ python: 'python3' }` and the plugin's own todo package | `python` names another interpreter |
| `pollMs` | `2000` | how often `inbox/` is read |
| `sessionEnv` | `DIRECTOR_LOCAL_SESSION_<hash of root>` | the variable that tags every process this DSH starts with the director's session; the plugin sets it itself. One per `root`, so two directors do not count each other's jobs |

## 3. Permissions

By default DSH lets the director and its workers write only inside the directory DSH was
started in (and temporary directories); a write anywhere else needs an approval, and this
profile has no client to approve it in, so it fails (`Permission denied`). Two ways to change
that:

- start DSH from a directory that contains every checkout the workers will change, or
- give the director full access: `DSH_PERMISSION_MODE=danger-full-access` in DSH's environment
  (the whole process), or `permissionPreset: danger-full-access` in the config above (the
  director's session; its workers inherit it).

Full access means the model can change and delete anything your user can, and run any
command, with nothing asking you first. Use it for a user account or machine you can lose.

## 4. Start it

```sh
export DEEPSEEK_API_KEY=...             # your key
dsh --profile director
```

It keeps running until you stop it (Ctrl-C). Started again, it resumes the same session, and
workers a restart interrupted are resumed. The first thing to check when nothing seems to
happen is `$ROOT/progress.jsonl`; a failed model call (no key, an unknown model) is also
printed to stderr as `dsh-director failed: ...`.

## 5. Give it work

Every file that lands in `$ROOT/inbox/` wakes the director once with its text, then moves to
`inbox/done/`. Name the checkout to work in (a git repository) and what done means:

```sh
cat > "$ROOT/inbox/task-1.json" <<'EOF'
{ "task": "In the git checkout /home/me/src/parser, make `npm test` pass; the failure is in test/dates.test.js." }
EOF
```

The director records the task as a work item in `work.json`, picks a template and freezes a
plan, then dispatches a worker for it. The templates the plugin ships, in
`todo/todo_templates/`:

| template | for |
|---|---|
| `change_files` | change named files, nothing outside a scope |
| `docs_change` | write or revise documentation files inside a scope |
| `code_change` | change code inside a scope and pass the repository's own test command |
| `test_fix` | make one failing test command pass, touching only a scope |
| `investigate` | write a JSON report with required keys |
| `find_verify` | read-only review: one finder per angle, then one verifier per finding |
| `cost_audit` | read-only audit of what the project's checks cost |

A plan is never improvised: when no template fits, add a YAML file next to these (copy the
closest one). `DSH_TODO_TEMPLATES=<dir>` in DSH's environment replaces the whole set with
the templates in `<dir>`.

When every item of a plan passed, the director finishes it: the worker's commits are in the
checkout, and a git bundle and a report are under `$ROOT/state/todo/finish/<attempt id>/`.

## 6. Watch and answer

Everything is under `$ROOT`:

| file | what is in it |
|---|---|
| `progress.jsonl` | one line per phase: `running`, `waiting`, `dormant`, or `failed` with the error |
| `work.json` | the work items, their state and who holds them |
| `notes.md` | the director's notes |
| `questions.json` | the director's questions to you, `open`, `answered` or `withdrawn` |
| `state/` | the plans and the runtime's own records; do not edit |

To answer an open question, drop its answer into the inbox (the director gets it as an event):

```sh
printf '%s\n' '{"answer": "Yes, drop Node 18."}' > "$ROOT/inbox/answer-<question id>.json"
```

The plugin also registers DSH commands for this (`/questions`, `/answer <id> <text>`), but the
profile above has no chat client to type them in; files are the way in.

## A steward instead of a director

The same two plugins can run a *steward*: one long-running assistant you talk to, which keeps
your memory and looks across your projects, and never does project work itself. Add these keys
to `director-local`'s `config`:

| key | what it does |
|---|---|
| `rootRole: steward` | the session named by `sessionId` is the steward, not a director |
| `memory: { userId: <name> }` | your memory: a git repository under `~/.local/share/dsh-director/memory/<name>/` (`root` puts it elsewhere; keep it outside anything mounted into containers). `timeZone` (an IANA name, default UTC) dates what it shows |
| `projectsDir` | optional: a directory whose every subdirectory is one local host root (a project) |

What a steward on this host can do:

- talk with you. Every turn you write to it in a DSH client attached to its session is recorded
  into your memory, with its replies in that turn, by the plugin itself; `memory_search` finds
  them, `memory_note` and `memory_profile_update` keep what it concluded or what you prefer. A
  file in `inbox/` wakes it as an event, like a director, and is not your words, so it is not
  recorded.
- with `projectsDir`: read every project there (`portfolio_projects`, `portfolio_project`,
  `portfolio_conversation`) and propose one new project (`propose`, kind `create_project`). You
  decide with `/proposals`, `/approve <id>` and `/decline <id> [reason]`; an approved project is a
  new subdirectory with `project.json`, `notes.md` and `work.json`. Nobody works on it until you
  start a director for it yourself (a profile of its own, `root` set to that subdirectory).
- ask you questions (`ask_owner`), which you answer with `/questions` and `/answer <id> <text>`, or
  a file `inbox/answer-<id>.json` as above.
- forget, restore or purge one memory entry only when you type `/forget <id> [reason]`,
  `/restore <id>` or `/purge <id>`; `/profile` shows your resident profile. Its own
  `memory_forget` refuses here (a DSH command carries no message of yours it could cite).

What it cannot do here, because this host does not provide it:

- pass anything to a director (no `handoff`), read a director's letters, or propose to start,
  stop, archive or unarchive a director or project;
- reach you anywhere but this DSH: no dashboard, no Slack or WeChat (the channel plugins do not
  load next to this host), no family groups, no daily brief and no reminders;
- use connections: no MCP servers through a catalog, no confirmations, no attachments, PDF or
  office files, no `make_document` or `calculate`.

Its prompt is cut to match: `steward.md` is written for the fullest host, and each passage about
something this host does not provide (the dashboard, chat channels, the brief, family groups,
handoffs and letters, the connection catalog) is left out or replaced with what to do here
(`lib/steward-prompt.mjs`). A host that provides everything gets `steward.md` unchanged.

## When it does not start

DSH prints two lines and exits:

```
dsh exit cause: dsh-director startup: Error
dsh-director startup: <the reason>
```

A failure that carries a system error code names that code on the first line instead of
`Error`, for example a `rolesFile` that does not exist:

```
dsh exit cause: dsh-director startup: connection-level ENOENT
dsh-director startup: ENOENT: no such file or directory, open '/nonexistent/roles.json'
```

The second line is the one to read.

| reason | fix |
|---|---|
| `missing config: provider` (or `model`, `root`) | add it to `director-local`'s `config` |
| `python not found` / `PyYAML is missing` | install python3 and PyYAML (Debian/Ubuntu: `sudo apt install python3-yaml`; elsewhere `python3 -m pip install -r requirements.txt`, or a venv); or set `todo.python` |
| `git not found` | install git |
| `cannot find @deepseek-ai/dsh-tools from config.dshEntry ...` | set `dshEntry` to the `lib/bin.js` of your DSH install |
| `Windows is not supported` | run it on Linux |

`pending (waiting for service: ...)` with no exit means the profile is not the one above.

## Checking this README

`standalone/scenario.mjs` runs this profile's shape (the same disabled tools, the same two
inserted plugins, no backend) with a scripted model instead of a real one: two tasks, two
plans, two workers, a watched background job, and dormancy at the end.

```sh
node standalone/scenario.mjs <your DSH>/lib/node_modules/@deepseek-ai/dsh/lib/bin.js
```
