extends RichTextEffect

var bbcode: String = "distort"

func _process_custom_fx(effect: CharFXTransform) -> bool:
    # Mutate actual glyph visibility and transform; font is read-only.
    if effect.relative_index % 2:
        effect.visible = false
    effect.transform = effect.transform.translated(Vector2(2, 0))
    return true
