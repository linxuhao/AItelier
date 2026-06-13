"""Shared step ID → label/phase mappings used across the TUI and scheduler.

Single source of truth so adding a new pipeline step requires only one edit.
"""

# step_id → coarse DPE phase (for completed_project_steps serialization)
COARSE_MAP: dict[str, str] = {
    "1": "1", "1_review": "1",
    "2": "2", "2_review": "2",
    "3": "3", "3_review": "3",
    "t_plan": "3", "t_plan_review": "3",
    "t_impl": "3", "t_impl_review": "3",
    "t_verify": "3", "t_verify_review": "3",
    "5": "5", "5_review": "5",
}

# step_id → human-readable label (for TUI status bar, dashboard, notifications)
STEP_NAMES: dict[str, str] = {
    "1": "Researcher", "1_review": "Research Review",
    "2": "Architect", "2_review": "Architecture Review",
    "3": "PM", "3_review": "PM Review",
    "t_plan": "Task Planner", "t_plan_review": "Plan Review",
    "t_impl": "Implementer", "t_impl_review": "Impl Review",
    "t_verify": "Verifier", "t_verify_review": "Verify Review",
    "5": "Final Verifier", "5_review": "Final Review",
}

# step_id set for steps that pause for human-in-the-loop checkpoint review.
# The TUI status bar uses this to show the correct placeholder message.
CHECKPOINT_STEPS: frozenset[str] = frozenset({"1", "2", "3"})
