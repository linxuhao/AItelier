extends Label

func _draw() -> void:
    draw_string(ThemeDB.fallback_font, Vector2(0, 100), "VISIBLE_TABLE_ID", HORIZONTAL_ALIGNMENT_LEFT, -1, 26, Color.WHITE)
