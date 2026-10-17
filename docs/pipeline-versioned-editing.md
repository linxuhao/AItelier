# Generated pipeline versions

`edit_pipeline`, `edit_role`, `edit_template` and coding-mode `config_edit`
automatically catalog the prior and next logical graph/role/prompt versions and
adopt a new immutable whole unit. No manual capture is required. Read the current
graph/source first; the response includes `unit_identity`, an optional
`expected_current` compare-and-swap input to every editor. A stale value refuses
before effects. Results distinguish the logical `version_id` (`sha256:…`), native
whole-unit CAS and the SDK integer `graph_version`.

The domain is registered, owned `gen_*` targets whose graph-referenced roles have
persisted owned definitions. Bare role names address the **current graph's**
logical role, including after successive edits. Explicit historical role names
are refused. Builtins, sealed `gen_rsi_*` bundles, shared template sources,
unreferenced/historical role edits, missing providers and `x-aitelier` host-policy
changes are outside this interface. `config_edit` accepts graph or roles source;
its replacement must occur exactly once. Role-table edits keep all keys and
historical entries unchanged. Use `edit_template` to detach a file-backed prompt
into a new inline role; it never modifies the shared template file.

`list_pipeline_versions` lists the verified native filesystem catalog;
`get_pipeline_version(version_id)` reads detached logical content/provenance.
`restore_pipeline_version(config, version_id, expected_current)` restores exact
logical graph, referenced settings/tools and UTF-8 prompts for future runs. All
historical aliases and graph rows remain, so existing runs retain their original
SDK graph pins and role definitions. Physical roles.json intentionally retains a
historical superset. Every adoption path (normal edits, restore and native
adoption) reuses an existing role name only for a canonically identical
definition (`true`, `1` and `1.0` stay distinct); a different existing definition
refuses before any seal, descriptor, file, registry or history effect, because
pinned runs resolve that name. Existing legacy roles that still name a shared template
continue to rely on that file remaining unchanged; normal editing never rewrites
it. No run is repinned.

A true no-op returns `changed=false` and writes no catalog entry, seal, graph
version, descriptor or file, even for an initially uncataloged target. Recorded
providers are checked before either no-op or changing success. Preflight
validation/domain/CAS/provider refusal has no effects.

After preparation starts, sealing and target publication are distinct durable
operations. An I/O/registration failure can leave immutable **prepared** seals;
the error reports their names/completeness and `publication_failed=true`, with
`changed=null` because this is not a successful edit or a claim of global rollback.
Native whole-state verification and the existing adoption descriptor determine
whether the previous unit is verified or the target remains pending and blocked.
Use native `reconcile_adoption(..., to='old'|'new')` for a pending unit; never clear
a descriptor or rewrite a sealed/historical unit by hand.

Normal operator edits have their own policy. The RSI optimizer's narrower
`derive_candidate` whitelist remains unchanged.
