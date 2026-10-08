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
var label := "ready"
var empty_label := ""
var maybe = null
# An UNSUPPORTED observed value: a Dictionary has no JSONable observation of its
# own, so reading it is a tagged read failure, never a str(v) string.
var stuff := {}


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

