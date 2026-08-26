# 素材生成纪律（capability: game_assets）

本任务被授予了 `gen_image_asset` / `gen_audio_asset`。它们只发给声明了这项能力的
任务卡，所以拿到它们就意味着这个任务要交付真实美术/音频，而不是占位图元。

- **视觉与音频交付真实资源，不要交付占位图元**：用 `gen_image_asset` 生成精灵与背景、`gen_audio_asset` 生成音效与背景音乐，资源落在 `assets/` 下，再在 `.tscn` 里引用。
  - 角色/障碍/道具：`gen_image_asset(prompt=…, dest="assets/bird.png", transparent=true, seed=<固定数>)`。**`transparent=true` 不是可选项**——生图模型没有 alpha 通道，你要"透明背景"它会把灰白棋盘格当不透明像素画出来；整屏背景才用 `transparent=false`。
  - 音效：`gen_audio_asset(dest="assets/sfx/flap.wav", kind="sfx", preset="jump", seed=<固定数>)`，preset 取 jump/coin/hit/explosion/powerup/laser/select/hurt。背景音乐用 `kind="bgm"` + `prompt`（上限 47s、单声道、**没有无缝循环点**，别在 README 里承诺完美循环）。
  - **所有精灵共用同一句风格描述**（例如一律追加 "pixel art, flat colors, 16-bit retro, side view"）。风格不统一是生成美术最容易露怯的地方。
  - **⚠️ 统一风格句里绝不能点名游戏对象**：风格句只写**画风**（媒介 / 用色方式 / 视角 / 线条），例如 "pixel art, flat colors, 16-bit retro, side view"。**不要**把物件清单写进去（像 "palette: sky blue background, green pipes, sandy ground, yellow bird" 这种）——生图模型会把清单里的每样东西都画进**每一张**图，结果就是 `ground.png` 里是一只鸟、`bird.png` 旁边杵着一根水管。每张图的主体只出现在它自己的 `prompt` 里。
  - **给 `gen_image_asset` 传 `subject`**（如 `subject="the scrolling ground strip"`）。工具会用视觉模型核对这张图画的**是不是**要的东西，不符就写进 `warning`。返回的 `warning` 非空时**必须重新生成**（换措辞、加 "only"/"nothing else"、或换 seed），不要把画错主体的资源接进场景。
  - **每个资源都传 `seed`**，重跑才能复现同一套美术。
  - `.tscn` 引用贴图：`[ext_resource type="Texture2D" path="res://assets/bird.png" id="2"]` + 节点上 `texture = ExtResource("2")`；音频用 `AudioStreamPlayer` + `[ext_resource type="AudioStream" path="res://assets/sfx/flap.wav" id="3"]` + `stream = ExtResource("3")`。**这些 ext_resource 块同样受实现约定里的 `.tscn` 块顺序约束**（`[gd_scene]` → 全部 `[ext_resource]` → 全部 `[sub_resource]` → 全部 `[node]`）。
  - 2D 渲染顺序用节点树顺序或 `z_index`。
  - **只有**当工具返回 `error`、或返回的 `warning` 提示抠图可能有问题时，才退回 `Polygon2D`/`ColorRect` 占位，并在 README 里写明哪一个是占位。
