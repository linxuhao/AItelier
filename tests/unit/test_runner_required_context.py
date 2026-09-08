from types import SimpleNamespace

import pytest

from aitelier.runner import AgentStepRunner


@pytest.mark.asyncio
async def test_required_context_missing_from_claim_never_starts_agent():
    """A resolver failure cannot degrade a scoped State job to generic work."""
    class DB:
        def get_repo_info(self, project_id):
            return {"repo_type": "none", "repo_path": None, "repo_url": None}

    class WS:
        def setup_workspace(self, *args, **kwargs):
            pass

    step = SimpleNamespace(
        run_context={"project_id": "sg-attempt"},
        step_id="implement",
        step_config={"agent_config": "offload_implementer", "context": [
            {"config": "coding_impl", "output": "plan.md", "required": True}
        ]},
        inputs={"_agent_config": {"name": "offload_implementer"}},
    )

    runner = AgentStepRunner(DB(), WS())
    with pytest.raises(RuntimeError, match="required context.*no resolved context"):
        await runner.execute(step)
