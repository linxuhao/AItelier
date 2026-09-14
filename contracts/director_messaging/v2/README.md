# Director messaging contract v2

This directory is the executable **statement** of the director-inbox lifecycle
contract. It imports every normative rule of the exact verified v1 contract at
`driver.inter-director-communication.contract@7` (contract hash
`feb4685515991484cb6182ba9824af0b368a239c547a86988289c7d5fd5711e1`) and adds
only the delta below. The wire schema is `aitelier.director-messaging.v2`.
Because v1 envelopes are closed, v1 clients must upgrade; v1 wire-schema
compatibility is intentionally not claimed.

This statement contains closed JSON schemas, a standalone transport-neutral
in-memory fake, and shared conformance/static tests. The real SQLite migration,
REST/MCP adapters, State wait integration, and PostCompact binding belong to a
separate proof attempt.

## Imported v1 contract

Unless this document explicitly changes a rule, v1 remains normative: its
request validation order, writer authorization, error codes and envelopes,
shared redactor, transaction and receipt-event behavior, routing and threads,
idempotency and CAS semantics, identifier/text bounds, broadcast limits,
ownership rules, and State wait behavior are unchanged. The authenticated
transport actor is authority. `director_identity` is provenance only, and
message content never authorizes work.

The four service actions remain:

```text
send_director_message(sender_project_id, director_identity, request_key,
                      subject, body, target_project_id=null, broadcast=false,
                      reply_to_delivery_id=null, delivery_mode="transient")
list_director_messages(project_id, after=0, limit=100,
                       delivery_mode=null, statuses=null)
acknowledge_director_message(project_id, delivery_id, expected_version,
                             request_key)
resolve_director_message(project_id, delivery_id, expected_version, request_key)
```

All four success/error envelopes now carry `aitelier.director-messaging.v2`.
Their nested objects remain closed. The message object adds required
`delivery_mode`, whose only values are `transient` and `standing`.

## Delivery mode and lifecycle

`send_director_message` accepts optional `delivery_mode`, defaulting to
`transient` after the unchanged validation/default expansion. The normalized,
default-expanded mode participates in the existing send idempotency payload. A
reply never inherits its parent's mode; omitting the field on a reply produces a
transient reply.

Both modes atomically create one message, one delivery, and exactly one v1
`director_message_received` actionable receipt event per target. The event
payload and summary rules are unchanged. No list, recovery projection,
acknowledgement, resolution, or idempotent replay creates another receipt event.

A transient message is retrieved only by explicit inbox listing and is never
injected automatically after compaction. A standing message is active exactly
while its delivery status is `unread` or `acknowledged`. It remains active across
service restart. It leaves automatic recovery only after the target project's
authenticated State writer performs the unchanged v1 CAS sequence
`unread(v1) -> acknowledged(v2) -> resolved(v3)`. Both modes, including resolved
rows, remain durably auditable through explicit listing.

The proof migration adds this column without destructive rewriting or a
transition-history table:

```sql
delivery_mode TEXT NOT NULL DEFAULT 'transient'
  CHECK(delivery_mode IN ('transient','standing'))
```

Existing rows become transient while retaining every existing ID, content,
time, delivery status/version/sequence, event, and dedupe record.

## Filtered high-water listing

The v1 `project_id`, `after`, and `limit` rules are unchanged: `after` is a
non-boolean integer at least zero; `limit` is a non-boolean integer from 1 to
100 and defaults to 100. v2 adds optional scalar `delivery_mode` and optional
`statuses`. A supplied mode is `transient` or `standing`. Supplied `statuses` is
a nonempty, duplicate-free list drawn from `unread`, `acknowledged`, and
`resolved`. Any invalid value returns the v2 `invalid_request` envelope.

In one read transaction, capture
`high = COALESCE(MAX(delivery_seq for project), 0)`. Consider only rows with
`after < delivery_seq <= high`; mode and status filters combine with that range
using AND. `matched_total` counts all matching rows in that snapshot. `items`
contains the first `min(limit, matched_total)` rows in ascending delivery
sequence. `has_more` is `matched_total > len(items)`. When `has_more` is true,
`next_after` is the last returned delivery sequence. Otherwise it is
`max(after, high)`, including empty and final pages. Listing never mutates state.

The authorized active-standing query is exactly:

```text
list_director_messages(project_id, after=0, limit=8,
                       delivery_mode="standing",
                       statuses=["unread", "acknowledged"])
```

## Bounded recovery projection

PostCompact performs one authorized active-standing query above. It never loads
full inbox history and never selects transient or resolved messages. From the
returned delivery-sequence-ascending items, it emits one compact line per item
using:

```python
json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
```

Each object has exactly `message_id`, `thread_id`, `delivery_id`,
`sender_project_id`, `delivery_seq`, `status`, `version`, `subject`, and
`body_excerpt`. Subject and body are passed independently through the v1 shared
redactor; the redacted body is then limited to 320 Python-`len` code points.

The whole section, including newline separators, is capped at 3000 Python-`len`
code points. Whole lines are appended in order while they fit. If any matched
item is omitted, trailing included lines are replaced as needed until the exact
marker `[omitted_active_standing=N]` fits, where
`N = matched_total - included`. No JSON line is truncated. Projection is
recovery context only: it never acknowledges or resolves a delivery and never
creates a receipt event.

## Statement verification boundary

`vectors.json` preserves the shared v1 behavioral scenarios with only the
expected wire/conformance versions changed; the required new closed response
fields are supplied by every result. Delta tests cover default and explicit
modes, reply noninheritance, invalid filters, sparse/empty/final high-water
pagination, restart-active standing messages, resolution removal from recovery,
redaction, project isolation, exact 8-item/3000-character projection, audit
retention, and absence of context or receipt-event replay.

The fake deliberately implements no SQLite, HTTP, MCP, State-wait, or real
PostCompact integration. Its `restart()` helper creates a new provider object
over the same in-memory persistence solely so the shared statement can express
the restart lifecycle. The later proof runs shared conformance vectors against
both this fake and the real provider.

Guidance for this protocol is **agent-neutral**. Codex, Claude, a workflow, and
an external harness are transport examples only. None owns the protocol,
supplies authorization through its name, or changes these rules.
