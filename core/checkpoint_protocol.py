"""Normal checkpoint views retain the SDK identity with the reviewed outputs."""
import hashlib
import json


def checkpoint_view(sf, run_id: str) -> dict:
    checkpoint = sf.get_checkpoint(run_id)
    expected = checkpoint["expected_checkpoint"]
    node = sf._get_resolver_for_run(run_id).get_node(checkpoint["step_id"])
    fixed = node.output_fixed if node else {}
    files = {}
    for slot, value in checkpoint["outputs"].items():
        spec = fixed.get(slot)
        name = spec.get("file") if isinstance(spec, dict) else spec
        # Only a fixed, non-wildcard slot names a retained file. Other engine
        # outputs remain visible by their actual key, never mutable disk data.
        if not isinstance(name, str) or any(c in name for c in "*?["):
            name = slot
        files[name] = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    digest = hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest()
    return {**checkpoint, "run_id": run_id, "checkpoint": checkpoint["step_id"],
            "step": checkpoint["step_id"],
            "checkpoint_instance": expected["step_instance_id"],
            "step_output": {"files": files},
            "decision_id": f"normal:{run_id}:{digest}"}


def approve_current_checkpoint(sf, run_id: str):
    """Only for an explicitly authorized automatic policy, never a manual POST."""
    observed = checkpoint_view(sf, run_id)
    return sf.approve_checkpoint(run_id, expected_checkpoint=observed["expected_checkpoint"],
                                 decision_id=observed["decision_id"])
