# core/interaction_meta.py
# Factory functions for building InteractionMeta based on conversation phase.
# Used by both api/meta_routers.py and web_api/meta_routers.py.

from models.schemas import InteractionMeta


def for_assessment_asking(turn: int, max_turns: int = 6) -> InteractionMeta:
    return InteractionMeta(
        phase="assessment",
        available_actions=["answer", "/skip", "/cancel"],
        hint="Type your answer, or use /skip to proceed with what you have so far.",
        turn=turn,
        max_turns=max_turns,
    )


def for_brief_review() -> InteractionMeta:
    return InteractionMeta(
        phase="brief_review",
        available_actions=["approve", "revise", "restart"],
        hint="Review the brief. Approve it or describe what to change.",
    )


def for_meta_conversation_asking(turn: int, max_turns: int = 6) -> InteractionMeta:
    return InteractionMeta(
        phase="meta_conversation",
        available_actions=["answer", "/skip", "/cancel"],
        hint="Answer the question or use /skip to proceed without a detailed brief.",
        turn=turn,
        max_turns=max_turns,
    )


def for_task_meta_asking(turn: int, max_turns: int = 4) -> InteractionMeta:
    return InteractionMeta(
        phase="task_meta",
        available_actions=["answer", "/skip"],
        hint="Describe what you want, or use /skip to force-generate the task spec.",
        turn=turn,
        max_turns=max_turns,
    )


def for_task_meta_complete() -> InteractionMeta:
    return InteractionMeta(
        phase="task_meta",
        available_actions=["view_task", "/status"],
        hint="Task spec completed. Use /status to check it.",
    )


def for_checkpoint_waiting(step_label: str = "Checkpoint", rejection_count: int = 0) -> InteractionMeta:
    hint = f"Review the {step_label} output. Approve to continue, or reject with feedback."
    if rejection_count > 0:
        hint = f"This step has been revised {rejection_count} time(s). " + hint
    return InteractionMeta(
        phase="checkpoint",
        available_actions=["approve", "reject"],
        hint=hint,
    )
