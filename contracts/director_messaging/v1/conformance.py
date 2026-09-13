"""Provider-neutral runner for literal director messaging conformance vectors."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from jsonschema import Draft202012Validator

from .fake import DirectorMessageError

HERE = Path(__file__).parent


def load_vectors(path: Path = HERE / "vectors.json") -> dict:
    vectors = json.loads(path.read_text(encoding="utf-8"))
    schema = json.loads((HERE / "vectors.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(vectors)
    return vectors


def _resolve(value, captures):
    if isinstance(value, str) and value.startswith("@capture:"):
        return captures[value.removeprefix("@capture:")]
    if isinstance(value, list):
        return [_resolve(item, captures) for item in value]
    if isinstance(value, dict):
        return {key: _resolve(item, captures) for key, item in value.items()}
    return value


def _pointer(document, pointer: str):
    value = document
    for part in pointer.strip("/").split("/") if pointer != "/" else []:
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def _assert_subset(actual, expected, path="$"):
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path}: expected object, got {type(actual).__name__}"
        for key, value in expected.items():
            assert key in actual, f"{path}: missing {key}"
            _assert_subset(actual[key], value, f"{path}.{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list), f"{path}: expected list"
        assert len(actual) == len(expected), f"{path}: list length differs"
        for index, value in enumerate(expected):
            _assert_subset(actual[index], value, f"{path}[{index}]")
    else:
        assert actual == expected, f"{path}: {actual!r} != {expected!r}"


def run_vectors(harness, vectors: dict | None = None) -> int:
    """Run the same literal scenarios against any conforming provider harness.

    A harness supplies ``reset(project_ids)`` and ``for_actor(actor)``. Each
    returned actor-bound provider exposes exactly the four v1 action methods.
    """
    vectors = vectors or load_vectors()
    protocol_schema = json.loads((HERE / "schema.json").read_text(encoding="utf-8"))
    resolver = Draft202012Validator(protocol_schema)
    checks = 0
    for scenario in vectors["scenarios"]:
        harness.reset(vectors["project_ids"] if "project_ids" not in scenario
                      else scenario["project_ids"])
        captures = {}
        for step in scenario["steps"]:
            provider = harness.for_actor(step.get("actor", scenario["actor"]))
            arguments = _resolve(deepcopy(step["arguments"]), captures)
            expected = _resolve(deepcopy(step["expect"]), captures)
            try:
                actual = getattr(provider, step["action"])(**arguments)
            except DirectorMessageError as exc:
                actual = exc.as_dict()
            schema_name = step["schema"]
            resolver.evolve(schema={"$ref": f"#/$defs/{schema_name}",
                                    "$defs": protocol_schema["$defs"]}).validate(actual)
            _assert_subset(actual, expected)
            for name, pointer in step.get("capture", {}).items():
                captures[name] = _pointer(actual, pointer)
            checks += 1
    return checks
