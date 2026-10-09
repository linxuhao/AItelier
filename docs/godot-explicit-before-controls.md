# Capturing a value before causal input

Legacy `changed` and `unchanged` assertions keep their frame-zero baseline.
When a node appears later, use an explicit capture at an established,
input-free control point. The captured operand is a real, type-preserving
`_read_attr` result, not an authored value.

```yaml
- at: 110
  capture_before:
  - {id: ward_before_walk, node: Ward, attr: health, action_frame: 130}
  assert:
    CombatManager.phase: phase == "PLAYER_TURN"
- at: 130
  actions: [move_left]
- at: 800
  assert:
  - {name: ward_health_changed, node: Ward, attr: health,
     mode: changed, before: ward_before_walk}
```

The capture frame must precede a declared press or click, and that input must
precede the delta assertion. IDs are unique within one scenario invocation.
The explicit operand is a property access path: an identifier, optional dotted
properties, and literal integer or quoted-string indices (for example,
`profile.cultivation["month"]`). Arithmetic, predicates, calls, conditionals,
literal values, escaped index keys and dynamic indexing are refused. Both reads require the root
to occur in the resolved node's actual property list; a global or class constant
cannot masquerade as a node property. Legacy reads without `before` retain
their existing expression behavior.

The node and operand must match exactly. A logical node may be recreated
between legs of a scenario; its earlier value remains immutable. Every
capture receipt records scenario, frame, action frame, operand, and actual
typed value. Failed/missing/duplicate captures, wrong bindings, unsupported
values, and invalid order fail closed. A dispatched input that changes nothing
still fails `changed`; it passes `unchanged` only when both actual reads match.

Do not use the first later observation as a substitute baseline. Keep existing
product input frames, state assertions, numeric requirements and zero-delta
controls. CPU admission tests do not execute Godot or establish runtime proof.
