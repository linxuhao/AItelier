extends Node
# The minimal built-in text surfaces the native text-observation pilot observes.
# Every surface here is a stock Control with NO script of its own, so the
# observer has to see it by walking the live tree rather than by any script
# census. Two literal strings ride in the captured corpus:
#   * a Label named with a stable VISIBLE ID ("VisibleIdLabel") whose text is a
#     literal, untranslated string -> the observer records the source text.
#   * a Button whose text is a translation KEY that is not in any loaded catalog
#     -> the engine's translate() returns it UNCHANGED, so the literal key is
#     what is displayed and the observer must report it as resolved, not as an
#     unresolved translation.
#
# This fixture is an OBSERVATION target only: it drives no assertion and its
# strings are not proof of translation coverage.
func _ready() -> void:
	var root := Control.new()
	root.name = "TextRoot"
	root.set_anchors_preset(Control.PRESET_FULL_RECT)
	add_child(root)
	var label := Label.new()
	label.name = "VisibleIdLabel"
	label.text = "VISIBLE_TABLE_ID"
	root.add_child(label)
	var button := Button.new()
	button.name = "MissingKeyButton"
	button.text = "missing_catalog.key"
	root.add_child(button)

	# Unicode + localized display text, under a plain Node intermediary whose
	# parent is fully transparent AND hidden. Official 4.7.2 CanvasItem: a
	# CanvasItem whose parent is not a CanvasItem is parented to the canvas
	# itself, so neither modulate nor visibility crosses the plain Node. This
	# Label is a resolved, visible surface (positive case for the chain end).
	var veiled := Control.new()
	veiled.name = "VeiledHiddenRoot"
	veiled.modulate = Color(1, 1, 1, 0.0)
	veiled.visible = false
	root.add_child(veiled)
	var intermediary := Node.new()
	intermediary.name = "NonControlIntermediary"
	veiled.add_child(intermediary)
	var unicode_label := Label.new()
	unicode_label.name = "UnicodeLocalizedLabel"
	unicode_label.text = "战况表 · localized_display"
	unicode_label.position = Vector2(24, 120)
	intermediary.add_child(unicode_label)

	# A fully hidden Label: visible_in_tree is false, so its text is never
	# fabricated as visible.
	var hidden_label := Label.new()
	hidden_label.name = "HiddenLabel"
	hidden_label.text = "hidden_unreachable_text"
	hidden_label.position = Vector2(24, 168)
	hidden_label.visible = false
	root.add_child(hidden_label)

	# A Label fully outside a clipping ancestor: the whole rect is clipped
	# away, so the effective intersection is empty.
	var clip_panel := Panel.new()
	clip_panel.name = "ClipPanel"
	clip_panel.position = Vector2(24, 216)
	clip_panel.size = Vector2(64, 32)
	clip_panel.clip_contents = true
	root.add_child(clip_panel)
	var clipped_label := Label.new()
	clipped_label.name = "ClippedOutLabel"
	clipped_label.text = "clipped_out_text"
	clipped_label.position = Vector2(200, 0)
	clip_panel.add_child(clipped_label)

	# A Label whose effective (self x ancestor) alpha is 0: it cannot bear
	# visible text.
	var faded_root := Control.new()
	faded_root.name = "FadedRoot"
	faded_root.modulate = Color(1, 1, 1, 0.0)
	faded_root.position = Vector2(24, 264)
	root.add_child(faded_root)
	var faded_label := Label.new()
	faded_label.name = "FadedLabel"
	faded_label.text = "fully_transparent_text"
	faded_root.add_child(faded_label)

	# OWN self_modulate zero: self_modulate affects ONLY the item itself, so
	# this Label's own text cannot be visible (negative case, own alpha).
	var own_faded := Label.new()
	own_faded.name = "OwnSelfModulateZeroLabel"
	own_faded.text = "own_self_modulate_zero_text"
	own_faded.self_modulate = Color(1, 1, 1, 0.0)
	own_faded.position = Vector2(24, 312)
	root.add_child(own_faded)

	# PARENT-only self_modulate zero: self_modulate does NOT inherit, so the
	# child Label below stays visible even though its parent is fully faded
	# (positive case, direct CanvasItem chain only).
	var parent_faded := Control.new()
	parent_faded.name = "ParentSelfModulateZero"
	parent_faded.self_modulate = Color(1, 1, 1, 0.0)
	parent_faded.position = Vector2(24, 360)
	root.add_child(parent_faded)
	var still_visible := Label.new()
	still_visible.name = "ParentSelfModulateChildLabel"
	still_visible.text = "parent_self_modulate_does_not_inherit"
	parent_faded.add_child(still_visible)

	# top_level: the rendering parent becomes the canvas, so the Label escapes
	# its parent's clip_contents and modulate (positive case), while visibility
	# still follows the scene-tree parent (TopLevelHiddenParent: negative case).
	var tl_clip := Panel.new()
	tl_clip.name = "TopLevelClipFadedParent"
	tl_clip.position = Vector2(300, 0)
	tl_clip.size = Vector2(8, 8)
	tl_clip.clip_contents = true
	tl_clip.modulate = Color(1, 1, 1, 0.0)
	root.add_child(tl_clip)
	var top_label := Label.new()
	top_label.name = "TopLevelLabel"
	top_label.text = "top_level_text"
	top_label.top_level = true
	top_label.position = Vector2(24, 408)
	tl_clip.add_child(top_label)
	var tl_hidden := Control.new()
	tl_hidden.name = "TopLevelHiddenParent"
	tl_hidden.visible = false
	root.add_child(tl_hidden)
	var top_hidden := Label.new()
	top_hidden.name = "TopLevelUnderHiddenLabel"
	top_hidden.text = "top_level_under_hidden_text"
	top_hidden.top_level = true
	top_hidden.position = Vector2(200, 408)
	tl_hidden.add_child(top_hidden)

	# Nested viewport: a Label inside a SubViewport whose on-screen geometry
	# cannot be resolved against the captured viewport -> explicitly
	# unresolved, never guessed.
	var svc := SubViewportContainer.new()
	svc.name = "NestedViewportContainer"
	svc.position = Vector2(24, 456)
	svc.size = Vector2(128, 64)
	root.add_child(svc)
	var svp := SubViewport.new()
	svp.size = Vector2(128, 64)
	svc.add_child(svp)
	var nested := Label.new()
	nested.name = "NestedViewportLabel"
	nested.text = "nested_viewport_text"
	svp.add_child(nested)

	# Label._shape transforms the drawn string: uppercase maps it through the
	# TextServer, and visible_characters (VC_CHARS_BEFORE_SHAPING, the default)
	# keeps only the first characters. Both are observed as drawn.
	var upper := Label.new()
	upper.name = "UppercaseLabel"
	upper.text = "upper_case_text"
	upper.uppercase = true
	upper.position = Vector2(300, 120)
	root.add_child(upper)
	var partial := Label.new()
	partial.name = "VisibleCharactersLabel"
	partial.text = "visible_characters_text"
	partial.visible_characters = 4
	partial.position = Vector2(300, 168)
	root.add_child(partial)

	# A clip_contents rect narrower than 0.5 px: the renderer does not draw the
	# clipping item or its children, although the logical intersection is > 0.
	var thin := Control.new()
	thin.name = "SubPixelClip"
	thin.position = Vector2(300, 216)
	thin.size = Vector2(0.4, 32)
	thin.clip_contents = true
	root.add_child(thin)
	var thin_label := Label.new()
	thin_label.name = "SubPixelClippedLabel"
	thin_label.text = "sub_pixel_clipped_text"
	thin.add_child(thin_label)
