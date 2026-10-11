"""Identity of the finite Writing Bench implementation loaded by this process."""
from __future__ import annotations

import hashlib
import importlib
import json
import sys
from types import CodeType

__bench_implementation_code__ = sys._getframe().f_code

# Keep the previous protected source boundary, including this identity helper.
MODULES = ("aitelier.writing_bench", "aitelier.writing_bench.adapter",
           "aitelier.writing_bench.bench", "aitelier.writing_bench.context",
           "aitelier.writing_bench.reading", "aitelier.writing_bench.storage",
           "aitelier.writing_bench.identity", "aitelier.novel_state",
           "core.dpe_pipeline", "core.ai_router")


def _constant(value):
    if isinstance(value, CodeType):
        return _code(value)
    if isinstance(value, tuple):
        return ["tuple", [_constant(item) for item in value]]
    if isinstance(value, frozenset):
        return ["frozenset", sorted((_constant(item) for item in value), key=repr)]
    return ["literal", type(value).__name__, repr(value)]


def _code(code):
    # Include nested executable code and its constants, without reading source.
    fields = ("co_argcount", "co_posonlyargcount", "co_kwonlyargcount",
              "co_nlocals", "co_stacksize", "co_flags", "co_code", "co_names",
              "co_varnames", "co_filename", "co_name", "co_qualname",
              "co_firstlineno", "co_linetable", "co_exceptiontable",
              "co_freevars", "co_cellvars")
    return {**{name: _constant(getattr(code, name)) for name in fields},
            "co_consts": [_constant(value) for value in code.co_consts]}


def executing_identity():
    # Resolve lazy host modules before freeze, not for the first time after review.
    loaded = [importlib.import_module(name) for name in MODULES]
    codes = {}
    for module in loaded:
        code = getattr(module, "__bench_implementation_code__", None)
        if not isinstance(code, CodeType):
            raise RuntimeError("Writing Bench implementation capture missing: " + module.__name__)
        codes[module.__name__] = _code(code)
    return hashlib.sha256(json.dumps(codes, sort_keys=True).encode()).hexdigest()
