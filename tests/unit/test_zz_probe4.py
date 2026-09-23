import ast
import importlib.util
import json
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "tt", ROOT / "tests/unit/test_truncation_is_not_a_formatting_mistake.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

KEY_SET = ('"file"', '"sha"', '"from_line"', '"to_col"', '"new_text"')


def _show(rev, path):
    out = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=ROOT,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _sentences(text):
    flat = re.sub(r"\s+", " ", text)
    return re.split(r"(?<=[.!?。；;])\s*", flat)


def new_violations(name, text):
    out = []
    for s in _sentences(text):
        low = s.lower()
        if "hunk" not in low:
            continue
        if "stale" not in low and "ambiguous" not in low:
            continue
        has_reread = any(w in s for w in ("reread", "re-read", "重读", "重新读"))
        has_sha = "sha" in low or "citation" in low
        has_place = any(w in s for w in ("range", "那一段", "窗口", "citation"))
        if not (has_reread and has_sha and has_place):
            out.append(f"stale_hunk_remedy: {name}: {s[:90]!r}")
    if name in ("GUIDANCE_EN", "GUIDANCE_ZH"):
        miss = [k for k in KEY_SET if k not in text]
        if miss:
            out.append(f"reference_key_set_incomplete: {name}: missing {miss}")
    if name == "templates/fix_tests.md":
        miss = [k for k in ("references", "citation", "sha", "raw=true")
                if k not in text]
        if miss:
            out.append(f"reference_mode_teaching_incomplete: {name}: "
                       f"missing {miss}")
    for token, kind in (("：", "colon"), (":", "ascii_colon")):
        pass
    idx = text.find('{"file"')
    if idx >= 0 and name in ("GUIDANCE_EN", "GUIDANCE_ZH"):
        before = text[:idx].rstrip()
        if not before.endswith((":", "：")):
            out.append(f"unannounced_reference_example: {name}: the example "
                       f"starts after {before[-24:]!r}")
    return out


def test_probe4():
    parts = []
    corpus = mod._prose_corpus()
    for name, text in sorted(corpus.items()):
        v = new_violations(name, text)
        if v:
            parts.append(f"{name}: {v}")
    parts.append(f"--- surfaces: {len(corpus)}")
    # per-file detail for template surfaces containing '{"file"'
    for name, text in sorted(corpus.items()):
        if name.startswith("templates/") and '{"file"' in text:
            idx = text.find('{"file"')
            parts.append(f"FKEY {name}: ...{text[:idx].rstrip()[-40:]!r}")
    raise AssertionError("\n".join(parts))
