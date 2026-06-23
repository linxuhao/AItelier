import { describe, it, expect, beforeAll, vi } from "vitest";
import { loadScript } from "./_loadScript.js";

let Utils;
beforeAll(() => {
  loadScript("utils.js");
  Utils = window.AItelier.Utils;
});

describe("Utils.escapeHtml (XSS guard)", () => {
  it("escapes all five HTML-significant characters", () => {
    expect(Utils.escapeHtml(`<a href="x" on=''>&`)).toBe(
      "&lt;a href=&quot;x&quot; on=&#39;&#39;&gt;&amp;",
    );
  });

  it("neutralises a script-injection payload", () => {
    const out = Utils.escapeHtml('<script>alert(1)</script>');
    expect(out).not.toContain("<script>");
    expect(out).toContain("&lt;script&gt;");
  });

  it("returns '' for null/undefined and coerces non-strings", () => {
    expect(Utils.escapeHtml(null)).toBe("");
    expect(Utils.escapeHtml(undefined)).toBe("");
    expect(Utils.escapeHtml(42)).toBe("42");
  });
});

describe("Utils.renderMarkdown (fallback path, no marked/DOMPurify)", () => {
  it("falls back to HTML-escaping when deps are absent", () => {
    // marked & DOMPurify are not loaded in the test env → safe fallback.
    const out = Utils.renderMarkdown("<img src=x onerror=alert(1)>");
    expect(out).not.toContain("<img");
    expect(out).toContain("&lt;img");
  });
});

describe("Utils.truncate", () => {
  it("leaves short strings untouched", () => {
    expect(Utils.truncate("hello", 10)).toBe("hello");
    expect(Utils.truncate("hello", 5)).toBe("hello"); // boundary: len == max
  });
  it("appends an ellipsis when over the limit", () => {
    expect(Utils.truncate("hello world", 5)).toBe("hello…");
  });
  it("handles null and negative/zero max", () => {
    expect(Utils.truncate(null, 5)).toBe("");
    expect(Utils.truncate("abc", 0)).toBe("…");
    expect(Utils.truncate("abc", -3)).toBe("…");
  });
});

describe("Utils.statusClass / statusIcon", () => {
  it("maps known statuses", () => {
    expect(Utils.statusClass("completed")).toBe("status-ok");
    expect(Utils.statusClass("running")).toBe("status-warn");
    expect(Utils.statusClass("failed")).toBe("status-err");
    expect(Utils.statusIcon("completed")).toBe("✓");
    expect(Utils.statusIcon("failed")).toBe("✗");
    expect(Utils.statusIcon("waiting_user_approval")).toBe("⏸");
  });
  it("falls back for unknown/empty", () => {
    expect(Utils.statusClass("nonsense")).toBe("");
    expect(Utils.statusClass("")).toBe("");
    expect(Utils.statusIcon("nonsense")).toBe("?");
    expect(Utils.statusIcon(null)).toBe("?");
  });
});

describe("Utils.formatTime (relative)", () => {
  it("buckets recent timestamps", () => {
    const ago = (s) => new Date(Date.now() - s * 1000).toISOString();
    expect(Utils.formatTime(ago(1))).toBe("just now");
    expect(Utils.formatTime(ago(30))).toBe("30s ago");
    expect(Utils.formatTime(ago(120))).toBe("2m ago");
    expect(Utils.formatTime(ago(2 * 3600))).toBe("2h ago");
    expect(Utils.formatTime(ago(3 * 86400))).toBe("3d ago");
  });
  it("returns '' for empty or invalid input", () => {
    expect(Utils.formatTime(null)).toBe("");
    expect(Utils.formatTime("")).toBe("");
    expect(Utils.formatTime("not-a-date")).toBe("");
  });
});

describe("Utils.debounce", () => {
  it("collapses rapid calls into one trailing invocation", () => {
    vi.useFakeTimers();
    const fn = vi.fn();
    const d = Utils.debounce(fn, 100);
    d();
    d();
    d();
    expect(fn).not.toHaveBeenCalled();
    vi.advanceTimersByTime(100);
    expect(fn).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });

  it("preserves args and this-context of the last call", () => {
    vi.useFakeTimers();
    const fn = vi.fn();
    const ctx = { tag: "ctx" };
    const d = Utils.debounce(fn, 50);
    d.call(ctx, "a");
    d.call(ctx, "b");
    vi.advanceTimersByTime(50);
    expect(fn).toHaveBeenCalledTimes(1);
    expect(fn.mock.calls[0]).toEqual(["b"]);
    expect(fn.mock.instances[0]).toBe(ctx);
    vi.useRealTimers();
  });
});
