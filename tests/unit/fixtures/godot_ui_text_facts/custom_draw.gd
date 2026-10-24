extends Control

func _draw() -> void:
    draw_string(ThemeDB.fallback_font, Vector2(0, 30), "VISIBLE_TABLE_ID", HORIZONTAL_ALIGNMENT_LEFT, -1, 26, Color.WHITE)
