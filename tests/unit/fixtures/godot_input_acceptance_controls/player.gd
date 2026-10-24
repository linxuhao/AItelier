extends Node2D
@export var busy_frames: int = 11
@export var witness_only: bool = false
var grid_y: int = 0
var busy_until: int = -1
var input_acceptance: Dictionary = {"version": 1, "decisions": []}
# Application-owned raw observations are Dictionaries, omitted by the L0 walk.
# These count actual callbacks; they never supply acceptance decisions.
var raw_received: Dictionary = {"events": 0, "presses": 0, "tagged": 0, "untagged": 0, "bad_metadata": 0, "engine_press_frames": []}

func _count_received(event: InputEvent) -> void:
    raw_received["events"] += 1
    var token = event.get_meta("aitelier_input") if event.has_meta("aitelier_input") else null
    var eligible: bool = event is InputEventAction and event.pressed
    if eligible:
        raw_received["presses"] += 1
        raw_received["engine_press_frames"].append(Engine.get_process_frames())
        if token == null:
            raw_received["untagged"] += 1
    if token != null:
        if eligible and token is Dictionary and token.get("version") == 1 and token.get("run_id") is String and token.get("seq") is int:
            raw_received["tagged"] += 1
        else:
            raw_received["bad_metadata"] += 1

func acknowledge(event: InputEvent, decision: String) -> void:
    var token = event.get_meta("aitelier_input") if event.has_meta("aitelier_input") else null
    if token is Dictionary and token.get("version") == 1:
        input_acceptance["decisions"].append({"run_id": token["run_id"], "seq": token["seq"], "decision": decision})

func _unhandled_input(event: InputEvent) -> void:
    _count_received(event)
    if not event.is_action_pressed("move_up"):
        return
    if witness_only:
        acknowledge(event, "accepted") # Deliberately dishonest negative control: no gameplay mutation.
        return
    var now: int = Engine.get_process_frames()
    if now < busy_until:
        acknowledge(event, "rejected") # Actual busy refusal, before any movement mutation.
        return
    grid_y -= 1
    position.y = float(grid_y) # Actual movement commit.
    busy_until = now + busy_frames
    acknowledge(event, "accepted") # Only after commit.
