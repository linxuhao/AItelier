extends Node

func _ready() -> void:
    var canvas := RenderingServer.canvas_item_create()
    RenderingServer.canvas_item_set_parent(canvas, get_parent().get_canvas_item())
    ThemeDB.fallback_font.draw_string(canvas, Vector2(32, 130), "VISIBLE_TABLE_ID", HORIZONTAL_ALIGNMENT_LEFT, -1, 26, Color.WHITE)
    queue_free()
