using UnityEngine;

/// <summary>
/// Runtime-generated placeholder visuals — ZERO imported art, no .meta files, no
/// licensing, fully cross-platform. The DPE ships this canned file into a
/// generated Unity project so the game is playable immediately (see the project's
/// SceneBootstrapper). Swap for real art later by assigning Sprites/Materials in
/// the Inspector — these generated defaults are only used when nothing is assigned.
///
/// This file is intentionally dependency-free (only UnityEngine core) so it
/// compiles in any Unity project regardless of render pipeline or installed
/// packages.
/// </summary>
public static class Placeholders
{
    public enum Shape { Square, Circle }

    /// <summary>
    /// A solid-color sprite (square or circle) generated at runtime — use for any
    /// 2D entity (character, obstacle, pickup, …) until real art is dropped in.
    /// </summary>
    public static Sprite Sprite(Color color, Shape shape = Shape.Square,
                                int size = 64, float pixelsPerUnit = 64f)
    {
        var tex = new Texture2D(size, size, TextureFormat.RGBA32, false)
        {
            filterMode = FilterMode.Bilinear,
            wrapMode = TextureWrapMode.Clamp,
        };
        float radius = size * 0.5f;
        var pixels = new Color[size * size];
        for (int y = 0; y < size; y++)
        {
            for (int x = 0; x < size; x++)
            {
                bool inside = shape == Shape.Square;
                if (!inside)
                {
                    float dx = x + 0.5f - radius;
                    float dy = y + 0.5f - radius;
                    inside = dx * dx + dy * dy <= radius * radius;
                }
                pixels[y * size + x] = inside ? color : Color.clear;
            }
        }
        tex.SetPixels(pixels);
        tex.Apply();
        return UnityEngine.Sprite.Create(tex, new Rect(0, 0, size, size),
                                         new Vector2(0.5f, 0.5f), pixelsPerUnit);
    }

    /// <summary>
    /// A tinted primitive GameObject (Cube/Sphere/Capsule/Plane/Quad) — use for
    /// any 3D entity until a real mesh is dropped in (a Capsule is the convention
    /// for a placeholder character; Cube for obstacles, Sphere for pickups, …).
    /// </summary>
    public static GameObject Primitive(PrimitiveType type, Color color, string name = null)
    {
        var go = GameObject.CreatePrimitive(type);
        if (!string.IsNullOrEmpty(name)) go.name = name;
        var renderer = go.GetComponent<Renderer>();
        if (renderer != null) Tint(renderer, color);
        return go;
    }

    /// <summary>
    /// Tint a renderer across pipelines: built-in lit uses <c>_Color</c>, URP/HDRP
    /// use <c>_BaseColor</c>. Sets whichever the active material exposes.
    /// </summary>
    public static void Tint(Renderer renderer, Color color)
    {
        var mat = renderer.material;
        if (mat.HasProperty("_BaseColor")) mat.SetColor("_BaseColor", color);
        if (mat.HasProperty("_Color")) mat.SetColor("_Color", color);
    }
}
