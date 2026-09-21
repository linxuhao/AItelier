"""`apply_verdict(D, request)` must agree with `Depends(D)`, for any D.

Two rounds of this card each bought a LIST of dependency shapes the guard was
taught to handle, and each time the shape that was not on the list walked
through: an `async def` for r1, a `yield` dependency for r2. A third list would
buy the same thing a third time.

So this file does not assert what the guard does with a shape. It asserts that
the guard and an ordinary `Depends(...)` on an ordinary route reach the SAME
outcome and run the SAME code, for every dependency it can lay hands on. The
expected answer is never written down here: FastAPI supplies it. A shape nobody
thought of is therefore covered the moment it exists, because the comparison
does not need to know what it is.

Where the dependencies come from - none of the three is a list of shapes:

* a COMPOSITIONAL generator that crosses execution form, parameter source and
  outcome and then nests the result inside itself, so it emits combinations
  that were never enumerated;
* every dependency class `fastapi.security` publishes, instantiated by
  reflection - shapes written by FastAPI's authors, not by this repository;
* every authorization dependency `api.authz` publishes - the real verdicts this
  deployment runs.

Each source asserts its own size, so a source that silently stops producing
dependencies fails instead of passing over nothing.
"""
from __future__ import annotations

import ast
import functools
import inspect

import pytest
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.testclient import TestClient

from api import state_http
from api.state_http import apply_verdict

# What ran, in order, during one request. Both sides of the comparison are
# driven through a synchronous TestClient, one request at a time.
LEDGER: list[str] = []


# --- source 1: a compositional generator ------------------------------------

_FORMS = {
    "sync_def": "def D({p}):\n    LEDGER.append({lbl!r})\n    {body}\n",
    "async_def": "async def D({p}):\n    LEDGER.append({lbl!r})\n    {body}\n",
    "sync_generator": "def D({p}):\n    LEDGER.append({lbl!r})\n    {body}\n    yield None\n",
    "async_generator": "async def D({p}):\n    LEDGER.append({lbl!r})\n    {body}\n    yield None\n",
    "callable_object": ("class _C:\n    def __call__(self{sp}):\n        LEDGER.append({lbl!r})\n"
                        "        {body}\nD = _C()\n"),
    "async_callable_object": ("class _C:\n    async def __call__(self{sp}):\n        LEDGER.append({lbl!r})\n"
                              "        {body}\nD = _C()\n"),
    "class_constructor": ("class _C:\n    def __init__(self{sp}):\n        LEDGER.append({lbl!r})\n"
                          "        {body}\nD = _C\n"),
    "partial": ("def _f({p}):\n    LEDGER.append({lbl!r})\n    {body}\n"
                "D = functools.partial(_f)\n"),
    "lambda": "D = lambda {p}: (LEDGER.append({lbl!r}), {expr})[1]\n",
}

_PARAMETERS = {
    "none": "",
    "request": "request: Request",
    "path": "project_id: str",
    "query": "limit: int = 1",
    "header": 'x_probe: str = Header(default="")',
    "body": "payload: int = Body(embed=True)",
    "nested": "sub=Depends(SUB)",
}

_OUTCOMES = {
    "allow": ("pass", "None"),
    "refuse": ('raise HTTPException(403, "refused")', '_raise()'),
    "crash": ('raise ValueError("the verdict blew up")', '_boom()'),
}


def _raise():
    raise HTTPException(403, "refused")


def _boom():
    raise ValueError("the verdict blew up")


def _build(form: str, parameters: str, outcome: str, label: str, sub):
    body, expr = _OUTCOMES[outcome]
    params = _PARAMETERS[parameters]
    if parameters == "nested" and sub is None:
        return None
    if form == "lambda" and parameters != "none":
        # A lambda's parameter list carries no annotations, so it can only be
        # generated in its parameterless form; the other eight forms cover the
        # parameter sources.
        return None
    source = _FORMS[form].format(
        p=params, sp=(", " + params) if params else "", lbl=label, body=body, expr=expr)
    namespace = {"LEDGER": LEDGER, "Request": Request, "Header": Header, "Body": Body,
                 "Depends": Depends, "HTTPException": HTTPException,
                 "functools": functools, "SUB": sub, "_raise": _raise, "_boom": _boom}
    exec(compile(source, f"<generated {label}>", "exec"), namespace)
    return namespace["D"]


def _generated_dependencies():
    """Cross the axes, then nest the results inside each other.

    The second pass hands each dependency a sub-dependency drawn from the first
    pass, so what comes out is a composition - an async generator whose
    sub-dependency is a partial of a class constructor, and so on - rather than
    the axes themselves.
    """
    flat = []
    for form in _FORMS:
        for parameters in _PARAMETERS:
            if parameters == "nested":
                continue
            for outcome in _OUTCOMES:
                label = f"{form}|{parameters}|{outcome}"
                built = _build(form, parameters, outcome, label, None)
                if built is not None:
                    flat.append((label, built))
    nested = []
    picked = 0
    for form in _FORMS:
        for outcome in _OUTCOMES:
            # Walk the flat corpus with a stride that shares no factor with its
            # length, so the inner dependencies are spread across every axis
            # rather than clustered on the first few.
            inner_label, inner = flat[(picked * 37) % len(flat)]
            picked += 1
            label = f"{form}|nested({inner_label})|{outcome}"
            built = _build(form, "nested", outcome, label, inner)
            if built is not None:
                nested.append((label, built))
    return flat + nested


# --- source 2: the dependency classes FastAPI itself publishes ---------------

def _security_dependencies():
    import fastapi.security as security

    built = []
    for name in sorted(n for n in dir(security) if not n.startswith("_")):
        candidate = getattr(security, name, None)
        if not isinstance(candidate, type):
            continue
        for arguments in ({}, {"tokenUrl": "token"}, {"name": "x-api-key"},
                          {"authorizationUrl": "a", "tokenUrl": "token"},
                          {"flows": {}}, {"openIdConnectUrl": "https://example.invalid"}):
            try:
                instance = candidate(**arguments)
            except (TypeError, ValueError):
                continue
            # `SecurityScopes` and the credential models live in this module but
            # are not dependencies: FastAPI refuses to mount a non-callable as
            # one, so there is no `Depends(...)` outcome to compare against.
            # That the guard refuses them anyway is pinned separately, by
            # `test_a_verdict_that_cannot_run_refuses`.
            if callable(instance):
                built.append((f"fastapi.security.{name}", instance))
            break
    return built


# --- source 3: the verdicts this repository actually runs -------------------

def _repository_dependencies():
    from api import authz

    built = []
    for name in sorted(dir(authz)):
        if name.startswith("_"):
            continue
        candidate = getattr(authz, name)
        if not inspect.isfunction(candidate) or candidate.__module__ != authz.__name__:
            continue
        try:
            signature = inspect.signature(candidate)
        except (TypeError, ValueError):
            continue
        if any(p.annotation is Request or p.name == "request"
               for p in signature.parameters.values()):
            built.append((f"api.authz.{name}", candidate))
    return built


GENERATED = _generated_dependencies()
SECURITY = _security_dependencies()
REPOSITORY = _repository_dependencies()
CORPUS = GENERATED + SECURITY + REPOSITORY


def test_each_source_of_dependencies_produced_something():
    """A source that quietly stops producing would make every comparison below
    pass over nothing."""
    assert len(GENERATED) >= 100, len(GENERATED)
    assert len(SECURITY) >= 5, [n for n, _ in SECURITY]
    assert len(REPOSITORY) >= 2, [n for n, _ in REPOSITORY]


def _observe(app) -> tuple:
    """(was the request allowed, what ran) for one identical request."""
    with TestClient(app, raise_server_exceptions=False) as client:
        del LEDGER[:]
        response = client.post("/probe/p?limit=3", json={"payload": 7},
                               headers={"x-probe": "v"})
        return response.status_code < 400, tuple(LEDGER)


def _through_the_guard(dependency) -> tuple:
    app = FastAPI()

    @app.post("/probe/{project_id}")
    async def guarded(project_id: str, request: Request):
        await apply_verdict(dependency, request)
        return {"reached_the_handler": True}

    return _observe(app)


def _through_depends(dependency) -> tuple:
    app = FastAPI()

    @app.post("/probe/{project_id}", dependencies=[Depends(dependency)])
    async def plain(project_id: str):
        return {"reached_the_handler": True}

    return _observe(app)


@pytest.mark.parametrize("label,dependency", CORPUS, ids=[label for label, _ in CORPUS])
def test_the_guard_agrees_with_depends(label, dependency):
    """THE differential assertion.

    Nothing here says what the right answer is. `Depends(dependency)` on an
    ordinary route is the right answer, and `apply_verdict` has to reach it -
    the same allow-or-refuse, having run the same code. A dependency the guard
    discards instead of running (r2's `yield` hole: `200 | leaked: True` while
    `Depends` refused) fails on both halves at once.
    """
    assert _through_the_guard(dependency) == _through_depends(dependency), label


def test_the_guard_never_calls_the_dependency_itself():
    """The shape this card has been fixed twice for.

    `apply_verdict` must hand the dependency to FastAPI, not run it. A call of
    the parameter, or a dispatch on the shape of something it returned, is the
    defect itself coming back - so it is pinned in the source, where it cannot
    hide behind a shape nobody tested.
    """
    source = inspect.getsource(state_http.apply_verdict)
    tree = ast.parse(source)
    called = {node.func.id for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert "dependency" not in called, sorted(called)
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert not attributes & {"isawaitable", "isgenerator", "isasyncgen",
                             "iscoroutine", "isasyncgenfunction"}, sorted(attributes)
    awaited = {node.value.func.id for node in ast.walk(tree)
               if isinstance(node, ast.Await) and isinstance(node.value, ast.Call)
               and isinstance(node.value.func, ast.Name)}
    assert "dependency" not in awaited, sorted(awaited)


def test_a_verdict_whose_parameters_do_not_resolve_refuses():
    """Not only an uncallable verdict: one FastAPI can mount but cannot SOLVE on
    this request - a required query parameter nobody sent - is refused too. The
    request carries no `seat`, so the solve reports an error and the error is a
    refusal, not a default."""

    def needs_a_missing_query_parameter(seat: int):
        LEDGER.append("needs_a_missing_query_parameter")

    allowed, ran = _through_the_guard(needs_a_missing_query_parameter)
    assert allowed is False
    assert ran == ()


def test_dependency_overrides_reach_the_verdict():
    """The override is FastAPI's, applied by FastAPI, because the verdict is
    solved by FastAPI: `app.dependency_overrides[D]` changes the answer."""

    def refuses(request: Request):
        LEDGER.append("refuses")
        raise HTTPException(403, "refused")

    def allows(request: Request):
        LEDGER.append("allows")

    app = FastAPI()

    @app.post("/probe/{project_id}")
    async def guarded(project_id: str, request: Request):
        await apply_verdict(refuses, request)
        return {"reached_the_handler": True}

    assert _observe(app) == (False, ("refuses",))
    app.dependency_overrides[refuses] = allows
    assert _observe(app) == (True, ("allows",))


@pytest.mark.parametrize("unusable", [object(), 42, None, "require_reader"],
                         ids=["object", "int", "None", "str"])
def test_a_verdict_that_cannot_run_refuses(unusable):
    """The default is REFUSE with its own test: a verdict that never became a
    decision is not an approval."""
    allowed, _ = _through_the_guard(unusable)
    assert allowed is False, unusable
