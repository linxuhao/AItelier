"""The render routes serialise; the cheap headless routes do not.

Measured 2026-09-04: one unchanged tree swept 0 red alone and 6 red while a
second sweep ran, with disjoint red sets. Concurrency on the render path
turns the gate into a coin flip, so it is not allowed.
"""
import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[2] / "docker/godot/godot_harness.py"


def _handler_class():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and any(
                isinstance(b, ast.Name) and "Handler" in b.id or
                isinstance(b, ast.Attribute) and "Handler" in b.attr
                for b in node.bases):
            return node
    raise AssertionError("no request-handler class in godot_harness.py")


def test_the_render_routes_are_named_and_include_playtest():
    cls = _handler_class()
    routes = None
    for node in cls.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "_RENDER_ROUTES" for t in node.targets):
            routes = {ast.literal_eval(e) for e in node.value.elts}
    assert routes is not None, "_RENDER_ROUTES must list the routes that render"
    assert "/playtest" in routes, "the play-test sweep is the gate that flaked"
    assert "/compile" not in routes, "compile is headless and cheap; keep it concurrent"


def test_the_lock_is_class_level_so_all_threads_share_it():
    cls = _handler_class()
    assert any(isinstance(n, ast.Assign)
               and any(getattr(t, "id", "") == "_RENDER_LOCK" for t in n.targets)
               for n in cls.body), (
        "_RENDER_LOCK must be a CLASS attribute — ThreadingHTTPServer builds a new "
        "handler INSTANCE per request, so a per-instance lock serialises nothing")


def test_the_lock_is_released_on_every_path():
    src = SRC.read_text(encoding="utf-8")
    i = src.index("held = self.path in self._RENDER_ROUTES")
    body = src[i:i + 2000]
    assert "finally:" in body and "_RENDER_LOCK.release()" in body, (
        "release must be in a finally — a route that raises would otherwise wedge "
        "every later sweep behind a lock nobody holds any more")
