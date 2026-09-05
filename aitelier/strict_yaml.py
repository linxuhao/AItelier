"""Read authored YAML contracts where a REPEATED MAPPING KEY IS AN ERROR.

yaml.safe_load applies last-one-wins to a repeated key, so a scenario file that
carries two ``assert:`` blocks under one timeline entry loses one of them
silently and the gate still reports "N/N passed" over whatever survived.
Measured on the game repo, 2026-09-05: 18 scenario files carried duplicate keys,
roughly 30 authored assertions were being discarded, and
cultivation/consequence_event_option_visible reported 9/9 while its ``changed``
assertion had been overwritten out of the contract before the run started.

Scope of the strictness, deliberately narrow:

* The loader subclasses SafeLoader FOR THIS CALL ONLY. yaml.SafeLoader is never
  mutated, so every other yaml user in the process keeps stock behaviour.
* Anchors, aliases and ``<<`` merge keys keep their legal semantics. Duplicates
  are detected on the authored mapping BEFORE merge flattening, so an explicit
  key that legally overrides a merged one is not reported, and a mapping may
  legally carry more than one merge key.
* Nothing else about parsing changes: valid input loads exactly as before.
"""

from __future__ import annotations

from pathlib import Path

import yaml


class DuplicateKeyError(yaml.YAMLError):
    """An authored mapping repeats a key, so the earlier value is discarded."""


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses a repeated mapping key. Never registered globally."""

    # Overwritten per call; only used to name the input in the error message.
    aitelier_source = "<yaml>"

    def construct_mapping(self, node, deep=False):
        seen: dict = {}
        for key_node, _value_node in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                # << is a directive, not data: repeats and overrides are legal.
                continue
            key = self.construct_object(key_node, deep=deep)
            try:
                first = seen.get(key)
            except TypeError:
                # Unhashable key. SafeConstructor reports that itself, below.
                continue
            if first is not None:
                raise DuplicateKeyError(
                    "duplicate key %r in %s: line %d repeats line %d. YAML keeps "
                    "only the LAST value, so the earlier block is discarded and "
                    "anything it asserted never runs. Merge the two blocks, or "
                    "give one of them its own entry."
                    % (key, self.aitelier_source, key_node.start_mark.line + 1,
                       first.line + 1))
            seen[key] = key_node.start_mark
        return super().construct_mapping(node, deep=deep)


def load_yaml_strict(text: str, source: str = "<yaml>"):
    """safe_load(text), raising DuplicateKeyError on a repeated mapping key.

    ``source`` names the input in any error message (a path, or a label such as
    "inline_scenario"). Returns whatever safe_load would return.
    """
    loader = _StrictLoader(text)
    loader.aitelier_source = source
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def load_yaml_file_strict(path):
    """load_yaml_strict on a file, named by its own path in any error."""
    p = Path(path)
    return load_yaml_strict(p.read_text(encoding="utf-8"), source=str(p))
