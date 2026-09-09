# State DAG facets — build on contracts, never on implementations

Status: plan approved 2026-09-09; engine (Phase 1) + protocol (Phase 2) shipped
in the same change; coop-chain migration (Phase 3) applied through the live API;
the rest of the graph (Phase 4) waits for one real parallel round on coop.

## Why

Every dependency edge in `wuxia-myth` pointed at an implementation. `B depends on A`
meant *B cannot start until A's implementation is VERIFIED*, so the coop chain
(`host-authority → world-facts → encounter-isolation → {late-support,
authored-consequences, encounter-barrier}`) was four strictly serial rounds, and the
frontier width inside it was 1.

Prove2Me (arXiv:2608.28433) scales the same shape to 30,000 nodes with one rule:
*a proof may import statements, never proofs*. A statement is immutable, audited by
a human once, and every downstream proof builds on it; the implementation behind it
is nobody else's business. Translated to software: **you may build on a contract
(interface + stub/fake) or a design fact — never on another node's implementation.**
Composition of real implementations is a separate, explicit integration fact.

This was chosen over a "three sub-nodes per node" model (contract/test/content as
one triple with three parallel dependency lanes) because that model kept
`B.content → A.content`, which is exactly today's serialization on the expensive
lane, and it forked every node operation into three. A facet is a *label* on an
atomic node plus one edge rule; attempts, evidence, receipts, invalidation and the
UI do not know it exists.

## The rules

`state_nodes.facet ∈ {design, contract, test, content, integration}`, nullable.
A NULL facet is a legacy node and is exempt; the rules bind a node the moment it is
faceted, which is what lets a graph migrate one chain at a time, bottom-up.

**R1 — edges point at contracts.** A faceted node may depend only on `contract` or
`design` nodes. Two exceptions: `<k>` may depend on its own `<k>.test` /
`<k>.contract`, and an `integration` node may depend on anything (that is where
"A's real implementation composes with B's" lives — `validation.*`).
A faceted node may not depend on a legacy node: facet the dependency first.

**R2 — if you made them, they chain.** If `<k>.contract` exists, `<k>.test` (if it
exists) and `<k>` must depend on it. If `<k>.test` exists, `<k>` must depend on it.
This is contract → test → content, enforced by the existing "dependencies must be
VERIFIED before an attempt starts" rule — no ordering logic was added. Whether a
node *has* a test node is the director's call; R1 guarantees that anything built
upon has a contract.

Key suffixes `.contract` / `.test` must agree with the facet. Everything else may be
named freely.

Violations are rejected at `add_nodes` / `revise_node` / `set_node_facet` with a
message that names the fix (see `core/state_graph.py:facet_violations`), and the
same function backs the read-only `facet_lint` query so a migration can be checked
before it is applied.

## What each facet is verified against

| facet | deliverable | acceptance (cheap, director/independent agent) |
|---|---|---|
| design | a design fact | review (as today) |
| contract | interface + **stub/fake** — a contract without a fake is not a contract, nothing downstream can be tested against it | compiles; **read-back** attested |
| test | tests locatable by node key (`tests/<domain>/<key>/` or scenario prefix `<key>`), red/skip against the fake | exist + run; attested to cover the contract |
| content | the implementation | its `.test` passes; **VERIFIED means "satisfies its contract given its dependencies' contracts"** — a fact that stays true when a dependency's implementation later breaks |
| integration | nothing of its own | real implementations compose (playtest / e2e) |

**Read-back** is Prove2Me's sub-agent read-back: an independent agent is given only
the contract node's acceptance (not the goal prose, not the design docs) and writes
back what it requires; the director compares the two texts. Recorded as
`add_reference(kind="note", label="readback@r<revision>")`. It is required before a
contract or design node becomes a dependency target, and a read-back for an older
revision does not count. This is deliberately a protocol step recorded as a visible
note, not a required column: a non-empty field is a filled field, not an audit.

## Migration (Phase 3, coop chain)

`scripts/state_facets_migrate.py --project wuxia-myth --chain coop [--apply]`.
Dry-run prints the resulting nodes/edges and the `facet_lint` result; `--apply`
goes through the authorized HTTP API, bottom-up:

1. `design.*` → facet `design` via `set_node_facet` (no revision, VERIFIED kept).
2. For each of `host-authority`, `world-facts`, `encounter-isolation`,
   `authored-consequences`, `late-support`: add `<k>.contract` (depends on the
   contracts/designs the old node depended on), then `revise_node(<k>)` so its
   cross-node edges point at `.contract` nodes, then `set_node_facet(<k>, content)`.
3. `world-facts.test` is created as the one sample test node; other test nodes are
   the director's call.
4. `coop.reconnect` is left legacy: it depends on `growth.save-compat`, outside the
   chain. It is re-pointed when the growth chain migrates.

Cost accepted up front: `coop.host-authority`'s current CANDIDATE goes STALE because
its dependency snapshot changes. It carried no evidence and was never verified; the
same artifact commit can be re-registered as a new external attempt against the new
revision.

**The success metric is frontier width.** Before: 1 ready node in the chain. After
the contract nodes verify: `encounter-isolation`, `authored-consequences`,
`late-support` (and `world-facts` impl) ready together. If width does not change,
the plan is wrong — stop before Phase 4.

## Explicitly deferred

- A hard read-back gate on `start_attempt` — only if the protocol is seen skipped.
- UI grouping of `<k>` / `.contract` / `.test` into one card — domain filter first.
- Proof-sketch conditional acceptance (a parent closed before its children) — R1
  already buys most of the parallelism; that is a state-machine change.
- The playtest baseline rules (green→red / N-consecutive-red hard fail) — the paper's
  evidence says the leverage is on statement faithfulness, not detection.
