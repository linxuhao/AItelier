# Director messaging v3
The project mailbox remains one delivery per target project. New acknowledgements
are distinct registered-driver rows. at_least_n defaults to one; broadcast
snapshots target-project members. The cross-project broadcast boolean is a
different parameter. Transient quorum completion resolves atomically; standing
requires an explicit resolve.

V2 request arguments without new options retain closed v2 response projections.
Registered callers still use the quorum owner; first transient ack resolves.
Explicit protocol_version=v3, ack_mode/ack_quorum, or needs_my_ack requests select
the v3 closed response schema. Legacy unregistered single-driver adapters remain
v2. Same-project addressing is rejected by v3 with use_driver_inbox.
No historic actor is fabricated: migrated acknowledged/resolved deliveries carry
legacy_ack with empty acks.

Private driver inbox and dnote notebooks are separate private actions. They are
never projected into a public project read. Existing project note arguments keep
their in_force schema; state_graph_help publishes driver_arguments for each
private notebook action, allowing informational (default) and in_force.
