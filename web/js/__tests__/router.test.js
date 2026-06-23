import { describe, it, expect, beforeAll, beforeEach, vi } from "vitest";
import { loadScript } from "./_loadScript.js";

let Router;
beforeAll(() => {
  loadScript("router.js");
  Router = window.AItelier.Router;
});

/** A view stub recording show/hide calls. */
function makeView() {
  return { show: vi.fn(), hide: vi.fn() };
}

/** Set the hash and fire the hashchange event the router listens for. */
function go(hash) {
  window.location.hash = hash;
  window.dispatchEvent(new window.Event("hashchange"));
}

describe("Router.init validation", () => {
  it("rejects a non-array route table", () => {
    expect(() => Router.init("nope")).toThrow(/must be an array/);
  });
  it("rejects routes missing pattern or view methods", () => {
    expect(() => Router.init([{ pattern: "#/", view: {} }])).toThrow(
      /must implement show/,
    );
    expect(() =>
      Router.init([{ pattern: "#/", view: { show() {} } }]),
    ).toThrow(/must implement hide/);
    expect(() => Router.init([{ view: makeView() }])).toThrow(/pattern/);
  });
});

describe("Router matching", () => {
  let root, projects;
  beforeEach(() => {
    root = makeView();
    projects = makeView();
    window.location.hash = "#/";
    Router.init([
      { pattern: "#/", view: root },
      { pattern: "#/projects/{id}", view: projects },
    ]);
  });

  it("shows the root view for '#/'", () => {
    go("#/");
    expect(root.show).toHaveBeenCalled();
  });

  it("extracts path params for a parametrised route", () => {
    go("#/projects/42");
    expect(projects.show).toHaveBeenCalledWith({ id: "42" });
    expect(Router.currentRoute.view).toBe(projects);
    expect(Router.currentRoute.params).toEqual({ id: "42" });
  });

  it("hides the previous view when switching routes", () => {
    go("#/projects/7");
    expect(projects.show).toHaveBeenCalledWith({ id: "7" });
    go("#/");
    expect(projects.hide).toHaveBeenCalled();
    expect(root.show).toHaveBeenCalled();
  });

  it("redirects unmatched routes back to root", () => {
    go("#/does/not/exist");
    // _onHashChange rewrites the hash to '#/' → root becomes active.
    expect(window.location.hash).toBe("#/");
    expect(root.show).toHaveBeenCalled();
  });
});
