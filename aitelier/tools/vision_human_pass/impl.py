"""vision_human_pass — a person judged what the model could not see.

WHY THIS EXISTS
The readability gate has exactly two ways to say `passed: false`, and until now
they were indistinguishable to every reader downstream:

    blind = false   the game IS unreadable      -> go back and fix it
    blind = true    NOBODY LOOKED               -> find someone who can look

Both routed the same way — back to the PM — so on 2026-08-26 the PM was handed a
verdict that had judged 4 of 47 scenarios (all at full HP, before the connection
dropped) and planned a health-bar rewrite on it: change EMPTY_CAP_PX 14 -> 20,
citing an injured-frame answer that no model had ever produced. Two rounds of
work aimed at a defect nobody had seen, and the round finally died on
`Cycle limit exceeded` because a blind gate can never turn green by looping.

So the blind branch now ends at a checkpoint instead, and this tool records what
the person decided there.

WHAT APPROVAL MEANS
skillflow's `approve_checkpoint(run_id)` takes no payload; only `reject` carries
feedback. That asymmetry is not a limitation here, it is the contract:

    approve   "I looked at the frames. All six questions are fine."
    reject    "I looked. Here is what is wrong."  -> feedback, back to the PM

There is deliberately no third option meaning "proceed without looking". A gate
that can be waved through without evidence is the defect this gate exists to
catch, and it would be worse than the outage it replaces.

WHAT IT WRITES
It AMENDS the gate's own report rather than replacing it. `blind` and
`blind_reason` are kept exactly as the gate wrote them, so the trail reads
"the automated judge could not see, and a person judged instead" — never
"the gate was green all along".
"""

import json
from pathlib import Path


def vision_human_pass(vision_step: str = "5_vision",
                      workspace_root: str = "",
                      config_name: str = "",
                      out_dir: str = "",
                      **_ignored) -> dict:
    if not workspace_root or not config_name:
        return {"error": "workspace_root/config_name not injected — cannot "
                         "locate the gate's report to amend"}

    report_path = (Path(workspace_root) / config_name / vision_step
                   / "vision_report.json")
    if not report_path.is_file():
        return {"error": f"no vision_report.json at {report_path} — this step "
                         f"runs only on the gate's blind branch, so the gate's "
                         f"own report must already exist"}
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return {"error": f"vision_report.json at {report_path} is unreadable ({e})"}

    if not report.get("blind"):
        # Not a blind run: something routed here that should not have. Refuse
        # rather than stamp a human verdict over a verdict the model DID reach.
        return {"error": "vision_report.json is not blind (the model judged "
                         "these frames itself) — refusing to overwrite a real "
                         "verdict with a checkpoint approval"}

    was = report.get("blind_reason", "")
    report["passed"] = True
    report["judged_by"] = "human"
    report["human_approved_after"] = was
    report["summary"] = (
        f"Readability judged BY A PERSON at the gate's checkpoint. The "
        f"automated judge did not see these frames ({was}); the reviewer "
        f"approved, which in this pipeline means every checklist question was "
        f"answered YES by eye. `blind` is deliberately left true: the model's "
        f"failure to look is part of the record. "
        + str(report.get("summary", ""))[:400])

    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False),
                           encoding="utf-8")
    if out_dir:
        target = Path(out_dir)
        target.mkdir(parents=True, exist_ok=True)
        (target / "vision_report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    return {"written": "vision_report.json", "passed": True,
            "judged_by": "human", "amended": str(report_path),
            "summary": report["summary"][:200]}
