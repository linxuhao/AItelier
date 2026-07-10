# Unity → Godot game-harness migration

AItelier's agents were "blind" to Unity runtime problems: the compile gate caught
C# errors, but the PlayMode smoke test needed a ~14GB licensed editor image, was
slow/flaky, and gave little structured runtime insight. This migration replaces
the Unity harness with a Godot 4 one that is **strictly more agent-observable**,
and moves the game gate out of the base DPE pipeline into a composable **addon**.

## What was built

### ① Godot harness (`aitelier/tools/godot_{compile,playtest}` + `docker/godot`)
- `godot_compile` → `godot --headless --import` parse-checks every GDScript +
  scene; errors carry `res://` file + line. Chains `godot_playtest`. Preserves
  the `gate_skipped` fail-open→observable contract.
- `godot_playtest` → runs the main scene headless for N frames with an injected
  autoload **probe** that: caps fps for stable timing, auto-presses `ui_accept`
  so the game progresses, captures **runtime errors** (`SCRIPT ERROR`/`push_error`
  with file+line), and snapshots the **live scene-tree state** (each scripted
  node's variables + transform).
- `godot-builder` sidecar: python-slim + the free Godot 4 binary. **672MB, no
  license, no account, no secrets** — vs the Unity editor image's **14.3GB**.

### ② skillflow overlay / addon system (`skillflow.compose`)
A base graph declares named **anchors**; **addon** fragments inject steps
(`insert_after`) and wire context (`add_context`) at those anchors, resolved into
one graph *before* validation. The base DPE pipeline (`dpe_default.yaml`) is now
engine-agnostic; the Godot gate lives in `configs/addons/game_harness.yaml`,
spliced in at registration (`api/dependencies.py`). Swap engines (web, mobile)
by editing the base's `overlays:` list — never by forking the pipeline.

### Migration
Removed the Unity tools, sidecar, `Dockerfile.unity`, compose service, and
`UNITY_*` secrets. Rewrote the agent-facing templates (architect / implementer /
verifier / meta) for Godot conventions — dramatically simpler because Godot
`.tscn` scenes are **text**: no build-scene-in-code bootstrapper, no
`#if UNITY_EDITOR`, no bake menu, no reflection wiring.

Tests: 447 skillflow (12 new compose) + 589 AItelier unit (14 new Godot, incl.
real-binary integration) + 48 pipeline integration — all green.

## Drive: `flappy-bird-godot` vs `flappy-bird-unity`

A faithful Godot 4 port (same tuning: gravity, flap, pipe speed, spawn interval,
gap) driven through the real harness. The auto-playtest hovers the bird and
**scores 4 in 10s with zero runtime errors**; the state snapshot shows the agent
the live `score`, `state`, and bird `position`/`velocity_y`/`alive`. Injecting a
null-deref (invisible to the parse gate) is **caught by the playtest gate** with
`res://scripts/bird.gd line 32` — the exact runtime blindness that shipped 7 C#
errors in the Unity days.

| | Unity flappy bird | Godot flappy bird |
|---|---|---|
| Tracked files | 74 | **10** |
| Gameplay-script LOC | 1307 (12 `.cs`) | **196 (7 `.gd`)** |
| Total tracked LOC | 5833 | **322** |
| Scene delivery | rebuilt in code (`SceneBootstrapper`) + bake menu | **text `.tscn`, diffable, authored directly** |
| Placeholder art | `Placeholders` util (runtime `Texture2D` gen) | **built-in primitive nodes** (`Polygon2D`/`ColorRect`) |
| Builder image | 14.3 GB, licensed editor, account+2FA | **672 MB, no license/account/secrets** |
| Compile gate | Roslyn whole-repo C# compile | `--import` GDScript parse (file+line) |
| Runtime gate | PlayMode smoke (pass/fail, slow) | **headless run: errors w/ file+line + live state snapshot** |
| Agent sees runtime state | no | **yes** (`score`, `position`, `velocity`, `game_state`, …) |

The win isn't just smaller/cheaper — it's that a runtime problem now has a
precise, structured signal an autonomous agent can act on, which the closed Unity
toolchain could not provide.
