extends Control

@export var case_name: String = "positive"
var ready_done: bool = false
var sentinel: int = 73
var measured_locale: String = ""

func _label(text_value: String, where: Vector2 = Vector2(32, 32)) -> Label:
    var label := Label.new()
    label.name = "Text%d" % get_child_count()
    label.position = where
    label.size = Vector2(550, 48)
    label.add_theme_font_size_override("font_size", 26)
    label.text = text_value
    add_child(label)
    return label

func _paint_signal(target: Control) -> void:
    target.draw_string(ThemeDB.fallback_font, Vector2(0, 30), "VISIBLE_TABLE_ID", HORIZONTAL_ALIGNMENT_LEFT, -1, 26, Color.WHITE)

func _bitmap_token() -> ImageTexture:
    # Real bitmap glyphs; the eventual TextureRect has no native text property.
    var glyphs := {
        "V": [17,17,17,17,17,10,4], "I": [31,4,4,4,4,4,31],
        "S": [15,16,16,14,1,1,30], "B": [30,17,17,30,17,17,30],
        "L": [16,16,16,16,16,16,31], "E": [31,16,16,30,16,16,31],
        "_": [0,0,0,0,0,0,31], "T": [31,4,4,4,4,4,4],
        "A": [14,17,17,31,17,17,17], "D": [30,17,17,17,17,17,30]}
    var token := "VISIBLE_TABLE_ID"
    var image := Image.create(token.length() * 6, 7, false, Image.FORMAT_RGBA8)
    image.fill(Color(0.035, 0.045, 0.065, 1))
    for index in token.length():
        var rows: Array = glyphs[token[index]]
        for y in 7:
            for x in 5:
                if int(rows[y]) & (1 << (4 - x)):
                    image.set_pixel(index * 6 + x, y, Color.WHITE)
    return ImageTexture.create_from_image(image)

func _ready() -> void:
    var translation := Translation.new()
    translation.locale = "en"
    translation.add_message("start_key", "Start")
    TranslationServer.add_translation(translation)
    TranslationServer.set_locale("en")
    measured_locale = TranslationServer.get_locale()
    match case_name:
        "positive":
            _label("start_key")
        "visible_token":
            _label("VISIBLE_TABLE_ID")
        "missing_translation":
            var button := Button.new()
            button.name = "MissingKey"
            button.position = Vector2(32, 32)
            button.size = Vector2(400, 60)
            button.text = "missing_catalog.key"
            add_child(button)
        "hidden":
            _label("start_key")
            var hidden := _label("VISIBLE_TABLE_ID", Vector2(32, 110))
            hidden.hide()
        "hidden_bulk":
            _label("start_key")
            for index in 96:
                var hidden := _label("HIDDEN_BULK_%03d_" % index + "x".repeat(176), Vector2(32, 110))
                hidden.hide()
        "custom_draw":
            var custom := Control.new()
            custom.name = "CustomPaint"
            custom.position = Vector2(32, 100)
            custom.set_script(load("res://custom_draw.gd"))
            add_child(custom)
        "server_after_free":
            var drawer := Node.new()
            drawer.name = "FreedDrawer"
            drawer.set_script(load("res://server_draw.gd"))
            add_child(drawer)
        "rich_reveal":
            var rich := RichTextLabel.new()
            rich.name = "Rich"
            rich.position = Vector2(32, 32)
            rich.size = Vector2(550, 60)
            rich.bbcode_enabled = true
            rich.add_theme_font_size_override("normal_font_size", 26)
            rich.text = "[b]VISIBLE_TABLE_ID[/b]"
            rich.visible_characters = 16
            add_child(rich)
            var suffix := _label("VISIBLE_TABLE_IDsuffix", Vector2(32, 120))
            suffix.visible_characters_behavior = TextServer.VC_CHARS_BEFORE_SHAPING
            suffix.visible_characters = 16
        "rich_scroll":
            var rich := RichTextLabel.new()
            rich.name = "ScrolledRich"
            rich.position = Vector2(32, 32)
            rich.size = Vector2(550, 44)
            rich.bbcode_enabled = true
            rich.add_theme_font_size_override("normal_font_size", 26)
            rich.text = "Start\n\n\n\nVISIBLE_TABLE_ID"
            rich.scroll_active = true
            add_child(rich)
        "option_closed", "option_open":
            var option := OptionButton.new()
            option.name = "Options"
            option.position = Vector2(32, 32)
            option.size = Vector2(400, 60)
            option.add_item("Start")
            option.add_item("VISIBLE_TABLE_ID")
            option.select(0)
            add_child(option)
            if case_name == "option_open":
                option.get_popup().popup(Rect2i(32, 100, 400, 120))
        "scripted_label":
            var label := _label("Start")
            label.set_script(load("res://scripted_label.gd"))
            label.queue_redraw()
        "draw_signal":
            var label := _label("Start")
            label.draw.connect(_paint_signal.bind(label))
            label.queue_redraw()
        "bitmap":
            var image := TextureRect.new()
            image.name = "BitmapWords"
            image.position = Vector2(32, 80)
            image.size = Vector2(540, 42)
            image.texture_filter = CanvasItem.TEXTURE_FILTER_NEAREST
            image.texture = _bitmap_token()
            add_child(image)
        "rich_effect":
            var rich := RichTextLabel.new()
            rich.name = "EffectRich"
            rich.position = Vector2(32, 32)
            rich.size = Vector2(550, 60)
            rich.bbcode_enabled = true
            rich.add_theme_font_size_override("normal_font_size", 26)
            rich.install_effect(load("res://effect.gd").new())
            rich.text = "[distort]VISIBLE_TABLE_ID[/distort]"
            add_child(rich)
        "ambiguous":
            var tiny := _label("VISIBLE_TABLE_ID", Vector2(632, 355))
            tiny.add_theme_font_size_override("font_size", 2)
            tiny.clip_text = true
            tiny.size = Vector2(7, 4)
        "line_edit", "line_placeholder":
            var edit := LineEdit.new()
            edit.name = "Editable"
            edit.position = Vector2(32, 32)
            edit.size = Vector2(400, 60)
            edit.placeholder_text = "start_key"
            edit.text = "Typed literal" if case_name == "line_edit" else ""
            add_child(edit)
        "secret":
            var secret := LineEdit.new()
            secret.name = "Secret"
            secret.position = Vector2(32, 32)
            secret.size = Vector2(400, 60)
            secret.text = "VISIBLE_TABLE_ID"
            secret.secret = true
            add_child(secret)
        "oversize":
            _label("Q".repeat(1048576))
        "nodes":
            for index in 4097:
                var child := Node.new()
                child.name = "N%d" % index
                add_child(child)
            _label("Start")
        "depth":
            var parent: Node = self
            for index in 66:
                var child := Node.new()
                child.name = "D%d" % index
                parent.add_child(child)
                parent = child
    ready_done = true
