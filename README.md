# flappy-verify

A Godot 4 Flappy Bird clone with placeholder graphics — tap or press Space to flap the bird through scrolling pipes, score points, and restart instantly after game over.

## Quick Start

1. Open the project in **Godot 4.4+**.
2. Press **F5** (or click *Run Project*) — the game starts immediately.
3. **Tap / click** or press **Space** to flap the bird.

No manual scene setup, no imported assets — everything is self-contained.

## How to Play

| Action | Input |
|--------|-------|
| Flap | Space key or left mouse click/tap |
| Start game | Flap from the READY screen |
| Restart after death | Flap from the GAME OVER screen |

- Navigate the bird through the gaps between green pipes.
- Each pipe pair passed scores **1 point**.
- Hitting a pipe or the ground ends the game.
- The score is displayed at the top of the screen.

## Project Structure

```
flappy-verify/
├── project.godot              # Engine config: main scene, autoload, input map
├── scenes/
│   ├── main.tscn              # Main scene (bird, ground, background, HUD, camera)
│   └── pipe_pair.tscn         # Pipe pair packed scene (spawned by PipeSpawner)
└── scripts/
    ├── game_manager.gd        # Autoload singleton: game state & score
    ├── bird.gd                # Bird (CharacterBody2D): physics, input, death
    ├── pipe_pair.gd           # Pipe pair: scrolling, collision, scoring
    ├── pipe_spawner.gd        # Timer-based pipe instantiation
    ├── ground.gd              # Ground (StaticBody2D): placeholder texture
    └── hud.gd                 # HUD (CanvasLayer): score & message labels
```

## Design

- **State machine**: `READY` → `PLAYING` → `GAME_OVER`, managed by the `GameManager` autoload.
- **Physics**: The bird uses `CharacterBody2D` with `move_and_slide()`. Gravity is 980 px/s², flap impulse is −400 px/s.
- **Pipes**: Spawned every 1.8 seconds, scroll left at 150 px/s, and self-destruct when off-screen. Gap position is randomized within safe bounds.
- **Scoring**: A thin `Area2D` between each pipe pair increments the score once, then disables itself to prevent double-counting.
- **Restart**: `get_tree().reload_current_scene()` resets all state instantly.
- **Graphics**: All visuals are `ImageTexture` rectangles generated at runtime — no imported sprites.

## Technical Notes

- **Godot version**: 4.4 (mobile renderer).
- **Resolution**: 540×960, canvas_items stretch mode with aspect ratio kept.
- **Input action**: `flap` (bound to Space and left mouse button).
- **Placeholder colors**: Bird = yellow, Pipes = green, Ground = brown, Background = sky blue.
