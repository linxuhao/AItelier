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

The authorized active-standing read is a forward walk of:

```text
list_director_messages(project_id, after=cursor, limit=100,
                       delivery_mode="standing",
                       statuses=["unread", "acknowledged"])
```

beginning at `cursor = 0` and continuing with each page's `next_after` while
`has_more` is true. `matched_total` is the value on the page whose `after` was
zero, so it counts every matched row. The walk reads one page per 100 matching
rows and stops at the high-water mark.

## Bounded recovery projection

PostCompact performs the authorized walk above and keeps the NEWEST eight
matched standing deliveries by delivery sequence. It never selects transient or
resolved messages. From the kept rows, in ascending delivery sequence, it emits
one compact line per row using:

```python
json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
```

Each object has exactly `message_id`, `thread_id`, `delivery_id`,
`sender_project_id`, `delivery_seq`, `status`, `version`, `subject`, and
`body_excerpt`. Subject and body are passed independently through the v1 shared
redactor; the redacted body is then limited to 320 Python-`len` code points.

Revised 2026-11-21: the projection used to keep the first eight rows of a single
ascending page, which meant a project holding eight unacknowledged standing
deliveries could never inject a later one after a compaction - the newest rows
were exactly the ones left out, and the character budget then dropped the newest
kept row first. The walk, the newest window and the budget rule below changed
together; the listing rules above did not.

The whole section, including newline separators, is capped at 3000 Python-`len`
code points. Whole lines are appended in ascending delivery sequence while they
fit. If any matched row is omitted, the OLDEST included lines are dropped until
the exact marker `[omitted_active_standing=N]` fits, so the newest kept rows
survive the budget first, where `N = matched_total - included` over every row the
read matched. No JSON line is truncated. Projection is recovery context only: it
never acknowledges or resolves a delivery and never creates a receipt event.

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

Error `code` remains the closed machine-readable enum. `detail.message` is bounded
to 1000 characters and may explain a declared field and stable validation kind or
authorization requirement. It never echoes submitted values, unknown field names,
validator context, message body, credentials or the supplied director identity.
One exception is deliberate and named here so it is not read as a leak: a refusal
that is ABOUT a destination may name that destination project id, because the
sender already named it and cannot act on the refusal without it. Nothing else
from the request travels with it. An explanation does not relax request validation
or grant membership.
