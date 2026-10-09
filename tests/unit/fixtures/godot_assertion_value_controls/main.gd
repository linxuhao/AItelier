extends Node
# A tiny live project the native assertion-value controls run against. The vars
# exist so the probe's Expression evaluator has a real Boolean, number, String
# and a legitimately NULL attribute to observe:
#   * `score`  -- a number that advances every frame (both polarities).
#   * `label`  -- a non-empty String OBSERVATION (a value, not assertion truth).
#   * `empty_label` -- the EMPTY String, an advisory-false observation.
#   * `maybe`  -- a real null, unchanged across frames (a captured null baseline
#                 is NOT a missing baseline).
var score := 0
var input_health := 30
var input_stats := {"hp": 30}
var damage_inputs := 0

func _input(event: InputEvent) -> void:
	if event.is_action_pressed("before_damage"):
		damage_inputs += 1
		input_health -= 2
		input_stats["hp"] -= 2
	# before_noop is deliberately admitted input with no value mutation.
	# A changed delta on it must be false, even though the input was dispatched.

var label := "ready"
var empty_label := ""
var maybe = null
# UNSUPPORTED observed values: a live Object and a Callable have no observation
# of their own, so reading either is a tagged read failure, never a str(v)
# string. These are the false-green controls -- they must stay refused now that
# Color/Dictionary/Array/Vector2i/Rect2 are safely representable.
var stuff := RefCounted.new()
var cb := Callable()

# SAFELY representable structured values. Each carries its TYPE and KEY/VALUE
# distinctions through a tagged observation envelope, so an unchanged delta
# compares the value rather than a lossy string.
var cell := Vector2i(3, 4)          # unchanged across frames
var cell_moved := Vector2i(3, 4)    # moves to (5, 6) at score == 2
var box := Rect2(1, 2, 3, 4)        # unchanged
var tint := Color(1, 0, 0, 1)       # moves to blue at score == 2
var items := [1, 2, [3, 4]]         # nested; unchanged
var stats := {"hp": 10, "mp": 5}    # SAME entries, reordered at score == 2
var stats_moved := {"hp": 10}       # value changes at score == 2

# LOSSLESS numeric export: a Float observation is a tagged value in the engine's
# SCIENTIFIC form, so close finite values stay DISTINCT wire tokens. `ratio` is
# unchanged; `ratio_close` moves a single close finite step at score == 2.
var ratio := 1.0
var ratio_close := 1.0

# SCIENTIFIC-token Float controls: tiny, subnormal, min-normal and huge finite
# doubles each keep their exact value under the shortest round-trippable form,
# where a fixed 6-decimal (or 17-decimal) rendering would flatten or lose them.
# _ready supplies both the delta fields and their raw guards from the SAME
# decoded binary64 values before frame 0. These literals are initialization only.
var tiny := 1e-20                    # unchanged
var tiny_close := 1e-20              # steps to 2e-20 at score == 2 (a real change)
var subnormal := 5e-324              # literal token case (raw control below)
var minnormal := 2.2250738585072014e-308   # literal token case (raw control below)
var huge := 1.7976931348623157e308   # literal token case (huge_raw below)

# Raw guards and unchanged-token cases share the same genuine IEEE values.
# A positive guard on a separate value would still allow an unchanged-zero green.
var subnormal_raw := 0.0
var minnormal_raw := 0.0
var huge_raw := 0.0
var ieee_subnormal := 0.0

# +0.0 and -0.0 compare EQUAL in ordinary Godot numeric equality, so a raw sign
# difference must NOT become a legacy `changed`: `zero_neg` starts at +0.0 and
# flips to -0.0 at score == 2, yet both canonicalize to ONE token so the
# `unchanged` delta on it still passes.
var zero_pos := 0.0
var zero_neg := 0.0

# Two CLOSE FINITE Float KEYS in one Dictionary: the canonical key sort must keep
# them distinct and stable (a lossy sort token would collapse them). Same
# entries, reordered at score == 2 -> still an unchanged observation.
var keyed := {1.0: "a", 1.000000000000001: "b"}
# A TINY Float key that genuinely changes at score == 2: 1e-20 -> 1e-21 must be
# distinguishable under the scientific token (fixed-decimal would collapse both).
var tiny_key := {1e-20: "a"}

# Complete-encoding BYTE-cap controls (populated in _ready from runtime data).
# The cap is on the ACTUAL compact JSON encoding of the finished observation, so
# escaping is charged for real: a newline/backslash pair encodes to 4 bytes, not
# 2, and a multi-byte glyph is charged its real UTF-8 width rather than 1. Each
# OVER control has a genuinely-admitted BELOW twin, so the refusal is a real
# payload cap and not an arbitrary character/scan-pattern estimate.
var big_text := ""            # 70000 ASCII chars -> over the cap
var leaf_under := ""          # 50000 ASCII chars -> ~50002 bytes, under cap
var big_key := {}             # 70000-char String KEY -> over the cap
var key_under := {}           # 50000-char String KEY -> under cap
var byte_over_emoji := ""     # 18000 emoji -> 72002 UTF-8 bytes, over cap
var byte_under_emoji := ""    # 4000 emoji -> under cap under any escaping
var escape_over := ""         # 17000 newline/backslash pairs -> 68002, over
var escape_under := ""        # 15000 newline/backslash pairs -> 60002, under
var float_array_over := []    # 3000 tagged Floats -> ~90k, over cap
var float_array_under := []   # 800 tagged Floats -> ~24k, under cap
var vector_over := []         # 2500 tagged Vector2i -> ~82k, over cap
var vector_under := []        # 600 tagged Vector2i -> ~20k, under cap
# Typed KEY distinction (int 7, bool true, float 1.5 are distinct engine keys).
# The canonical key sort is stable, and the tagged Float key keeps the float
# key's TYPE visible instead of collapsing it onto a Python-style number.
var typed_keys := {7: "int", true: "bool", 1.5: "float"}        # unchanged
var typed_keys_moved := {7: "int", true: "bool", 1.5: "float"}   # value flip
# An UNTYPED var so it can hold an int at frame 0 and a float at score == 2:
# the tagged observation must treat the int->float TYPE change as a real change.
var typed = 1
var flag := true                        # bool at frame 0, false at score == 2
# NON-FINITE Float and vector/Color components have no legitimate observation:
# the official JSON path emits a fabricated null or an overflow, so the probe
# refuses them instead.
var blowup := INF
var tint_nan := Color(NAN, 0, 0, 1)
var cell_nan := Vector2(NAN, NAN)

# A self-referential Array is a cycle: the bounded walk refuses it at the depth
# bound instead of looping forever or stringifying it into an equal-looking value.
var looping := []


func _ready() -> void:
	# Set here (not as member initializers) because the sizes are runtime data.
	big_text = "x".repeat(70000)          # over cap
	leaf_under = "x".repeat(50000)        # ~50002 bytes, under cap
	big_key = {}
	big_key["k".repeat(70000)] = 1        # over cap (huge String KEY)
	key_under = {}
	key_under["k".repeat(50000)] = 1      # under cap
	# Emoji are 4 UTF-8 bytes each: 18000 -> 72002 encoded bytes (over the
	# 65536 cap) while a character count of 18000 would wrongly ADMIT it. The
	# literal glyph is used (not a surrogate escape) so Godot's UTF-32 String
	# holds ONE code point; 4000 emoji stay under the cap even if each encodes to
	# 12 bytes (\uXXXX).
	byte_over_emoji = "🟢".repeat(18000)
	byte_under_emoji = "🟢".repeat(4000)
	# A newline/backslash pair encodes to 4 bytes (\\n and \\\\): 17000 pairs ->
	# 68002 bytes (over), 15000 pairs -> 60002 bytes (under).
	escape_over = "\n\\".repeat(17000)
	escape_under = "\n\\".repeat(15000)
	# Tagged arrays: the per-element tagged envelope is counted in the actual
	# encoding, so each has a genuinely-admitted below-cap twin.
	float_array_over = []
	float_array_under = []
	for i in 3000:
		float_array_over.append(1e-20)
	for i in 800:
		float_array_under.append(1e-20)
	vector_over = []
	vector_under = []
	for i in 2500:
		vector_over.append(Vector2i(3, 4))
	for i in 600:
		vector_under.append(Vector2i(3, 4))
	looping = [1]
	looping.append(looping)
	# Stage the SAME genuine binary64 values for both delta cases and raw guards.
	# Decode at runtime before the observer captures its frame-0 baseline.
	# The admitted 4.7.2 native batch proved the `5e-324` and `2.2250738585072014e-308`
	# source literals stage as 0.0 on this engine, so a control that only echoed them
	# would be a vacuous unchanged-zero green. Each little-endian byte array below is
	# the exact double: [1,0,0,0,0,0,0,0] -> smallest positive subnormal (5e-324),
	# [0,0,0,0,0,0,16,0] -> smallest positive normal (2.2250738585072014e-308) and
	# [255,255,255,255,255,255,239,127] -> largest finite (1.7976931348623157e308).
	# The historical `subnormal_literal_staged_nonzero` scenario ID is kept for
	# evidence continuity; it now reads a genuine IEEE value supplied by the fixture,
	# not a guarantee that the source literal itself parsed nonzero.
	var sub_bits := PackedByteArray([1, 0, 0, 0, 0, 0, 0, 0])
	subnormal_raw = sub_bits.decode_double(0)
	subnormal = subnormal_raw
	var min_bits := PackedByteArray([0, 0, 0, 0, 0, 0, 16, 0])
	minnormal_raw = min_bits.decode_double(0)
	minnormal = minnormal_raw
	var huge_bits := PackedByteArray([255, 255, 255, 255, 255, 255, 239, 127])
	huge_raw = huge_bits.decode_double(0)
	huge = huge_raw
	var sub2_bits := PackedByteArray([1, 0, 0, 0, 0, 0, 0, 0])
	ieee_subnormal = sub2_bits.decode_double(0)


class LateNode extends Node:
	var ticker := 0

	func _process(_d):
		ticker += 1


func _spawn_late():
	# `LateNode` does not exist at frame 0, so a delta on LateNode|ticker has no
	# frame-0 baseline -- but its LATER read (frame 8) is genuinely valid, because
	# the node and the attribute exist by then. Spawn is frame-count deterministic
	# (never a wall clock), so the frame-8 assert cannot race it. The report must
	# say baseline_missing, not a current-read error: an absent frame-0 baseline is
	# its own fact.
	if get_node_or_null("LateNode") == null:
		var n := LateNode.new()
		n.name = "LateNode"
		add_child(n)


func _process(_d):
	score += 1
	# Frame 0 = the probe's baseline capture; spawn right after it, so the frame-0
	# baseline for LateNode|ticker was never captured while the later read is fine.
	if score == 2:
		_spawn_late()
		cell_moved = Vector2i(5, 6)
		tint = Color(0, 0, 1, 1)
		stats_moved = {"hp": 11}
		# Same entries as the frame-0 baseline, DIFFERENT insertion order: this is
		# NOT a value change, and an `unchanged` delta on `stats` must pass.
		stats = {"mp": 5, "hp": 10}
		# A close finite numeric step: 1.0 -> 1.000000000000001. Unchanged must
		# FAIL for `ratio_close` (a real value change) and pass for `ratio`.
		ratio_close = 1.000000000000001
		# A tiny-magnitude step: 1e-20 -> 2e-20 is a real change under the
		# scientific token (a fixed-decimal formatter would lose both).
		tiny_close = 2e-20
		# Same two close Float keys, reordered: still NOT a value change.
		keyed = {1.000000000000001: "b", 1.0: "a"}
		# +0.0 -> -0.0 is NOT a legacy value change (Godot numeric equality treats
		# them as equal), so `zero_neg` still passes its `unchanged` delta.
		zero_neg = -0.0
		# A TINY Float KEY change and an int->float / bool flip: each is a real
		# value change under the tagged observation.
		tiny_key = {1e-21: "a"}
		typed = 1.0
		flag = false
		typed_keys_moved = {7: "int", true: "bool", 1.5: "changed"}
