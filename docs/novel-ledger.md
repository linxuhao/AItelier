# Novel ledger: current state vs history (for drivers and agents)

A novel repo keeps two different things:

| What | Where | Who reads it |
|---|---|---|
| **Current state** — character cards (incl. `voice`), world, threads, arcs; overwritten every chapter | `novel/bible/` | Everyone, via the `state_probe` context pack (`novel_context.md`, step `probe`). This is what you write from. |
| **History** — one append-only row per change / appearance / hint / node, per chapter | `novel/ledger/<kind>/<entity>.jsonl`, kind ∈ `characters` `factions` `settings` `threads` `arcs` | Looked up **on demand** with the ledger query. Never pasted whole into a prompt. |

Rules for anyone writing or reviewing a chapter:

1. Current state is already in the context pack. Do not reconstruct it from history.
2. History questions ("尹骁的右臂第 8 章是什么样", "门这条伏笔前面暗示过什么",
   "邹建平上次出场是哪章") go through the ledger query — never by reading old
   `progression` / `setting_log` / `hints` blocks in bible files (those are
   pre-migration leftovers) and never by grepping whole chapters first.
3. Ask narrowly: one entity, a chapter range, a field. `limit` keeps the newest rows.

## Three ways to call it (same filters, same rows)

Filters: `kind`, `name`, `chapter`, `chapter_from`, `chapter_to`, `field`
(substring of a changed key, e.g. `右臂` also matches `右臂伤`), `entry_type`
(`event` `create` `appearance` `register` `hint` `resolve` `abandon`
`node_completed` `note`), `limit` (default 50, max 200).
Each row: `{kind, entity, chapter, type, changes?, reason?, detail?, node?, importance?}`.

**External driver — MCP** (`/mcp`, tool `novel_ledger_query`, private read: send
your driver token as for any private read, see `docs/driver-identity.md`):

```bash
AITELIER_DRIVER_ID=<id> python3 scripts/mcp_call.py novel_ledger_query \
  '{"project_id": "novel-init-0e349a76", "kind": "characters", "name": "尹骁", "field": "右臂", "chapter_from": 8, "chapter_to": 13}'
```

**External driver — HTTP** (`GET /api/projects/{project_id}/novel/ledger`, `require_reader`):

```bash
curl -s -H "X-AItelier-Driver-Token: $(cat ~/.aitelier-drivers/<id>.token)" \
  'http://127.0.0.1:4444/api/projects/novel-init-0e349a76/novel/ledger?kind=threads&name=门&entry_type=hint'
```

**Internal pipeline agent** — skillflow tool `ledger_query` (project root is
injected; enabled for the outline / draft / draft-review / finalize roles):

```
ledger_query(kind="characters", name="尹骁", field="右臂", chapter_from=8)
```

**Operator CLI** on the server: `python scripts/novel_ledger.py query <novel_repo> --kind characters --name 尹骁 --field 右臂`.

Migrating an old repo (history still inside bible files): `python scripts/novel_ledger.py migrate <novel_repo> --dry-run` first; it never commits. Migrate **before the first chapter booked after deploy** if you can (chapters booked earlier are kept and merged, but a pre-migration probe still shows the old flattened card keys). The ledger is rebuilt from `chapters/*/events.yaml`, so a migrated repo equals a fresh replay of its journals; `--stale-after N` is opt-in and breaks that equality (writing-bench repos must not use it). Re-running is a no-op once no legacy history block is left; the write is one transaction.
