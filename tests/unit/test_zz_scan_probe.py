import ast
import importlib.util
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]

spec = importlib.util.spec_from_file_location(
    "trunc_test", ROOT / "tests/unit/test_truncation_is_not_a_formatting_mistake.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def _text(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            else:
                parts.append("{}")
        return "".join(parts)
    return None


def test_scan():
    out = {"core_constants": [], "yaml_surfaces": []}
    for py in sorted((ROOT / "core").glob("*.py")):
        rel = f"core/{py.name}"
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            text = _text(node.value)
            if not names or text is None or "\n" not in text or len(text) < 120:
                continue
            for name in names:
                v = mod._prose_violations(f"{rel}:{name}", text)
                out["core_constants"].append(
                    [f"{rel}:{name}", len(text), v])
    for yml in sorted((ROOT / "agent_configs").glob("*.yaml")):
        try:
            doc = yaml.safe_load(yml.read_text(encoding="utf-8"))
        except Exception as exc:
            out["yaml_surfaces"].append([yml.name, "PARSE-FAIL", str(exc)[:80]])
            continue
        if not isinstance(doc, dict):
            out["yaml_surfaces"].append([yml.name, "NOT-DICT", ""])
            continue
        for role, cfg in doc.items():
            if isinstance(cfg, dict) and isinstance(cfg.get("system_prompt"), str):
                text = cfg["system_prompt"]
                v = mod._prose_violations(f"agent_configs/{yml.name}:{role}", text)
                out["yaml_surfaces"].append(
                    [f"agent_configs/{yml.name}:{role}", len(text), v])
    raise AssertionError(json.dumps(out, indent=1, ensure_ascii=False))
