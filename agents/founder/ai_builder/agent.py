import logging
from pathlib import Path
from typing import Any

from langgraph.errors import GraphBubbleUp
from pydantic import BaseModel

from core.registries import get_agent_registry
from core.state import VentureState
from workflows.ai_builder import compile_ai_builder

logger = logging.getLogger("afos.agents.ai_builder")

get_agent_registry().register_from_yaml(Path(__file__).parent / "manifest.yaml")

_VALID_DEPTHS = {"standard", "deep"}


class BuildReport(BaseModel):
    """The AI Builder Pipeline's single output contract - a summary of the Loop
    Controller-driven build run assembled entirely from the already-completed
    MVP Planner's plan and the reused Phase 2 Coding Engine agents' own results.
    """

    idea: str
    status: str = "pending"
    loop_status: str = "not_started"
    steps_total: int = 0
    steps_completed: int = 0
    iteration_count: int = 0
    debug_retry_invocations: int = 0
    step_progress: list[dict[str, Any]] = []
    summary: str = ""
    error: str = ""


def run_ai_builder(idea: str, venture_id: str, research_depth: str = "standard") -> dict:
    """Entry point for running the AI Builder Pipeline. Registered in the
    existing Agent Registry above; declares no permissions and calls the Tool
    Registry for nothing directly - it only ever reaches outside itself through
    the already-completed MVP Planner (Phase 4 Component 4) and the existing,
    unmodified Phase 2 Coding Engine agents it orchestrates (see
    workflows/ai_builder.py). Never raises for a genuine failure (GraphBubbleUp
    aside) - invalid input, a failed/unbuildable MVP Plan, and a failed build
    loop all degrade to a well-formed BuildReport with a descriptive status.

    Returns a dict shaped {"agent": ..., "event": ..., **BuildReport fields}.
    """
    depth = research_depth if research_depth in _VALID_DEPTHS else "standard"

    initial_state: dict[str, Any] = {
        "idea": idea,
        "venture_id": venture_id,
        "phase": "product",
        "research_depth": depth,
    }

    try:
        graph = compile_ai_builder()
        result = graph.invoke(initial_state)
        report = BuildReport(**result.get("build_report", {"idea": idea, "status": "failed"}))
        return {"agent": "ai_builder", "event": "ai_builder_result", **report.model_dump()}
    except GraphBubbleUp:
        raise
    except Exception as exc:
        logger.warning("ai_builder: unexpected pipeline failure for venture '%s': %s", venture_id, exc)
        return {
            "agent": "ai_builder",
            "event": "ai_builder_result",
            **BuildReport(idea=idea, status="failed", error=str(exc)).model_dump(),
        }


def ai_builder_node(state: VentureState) -> dict:
    """Manager-callable entry point in the same node shape every other agent uses.
    Not wired into any graph in this component. Uses idea/venture_id already
    present on VentureState - no extra state fields required.
    """
    entry = run_ai_builder(idea=state.get("idea", ""), venture_id=state.get("venture_id", "default"))
    return {"history": [entry]}
