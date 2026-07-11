import logging
from pathlib import Path
from typing import Any

from langgraph.errors import GraphBubbleUp
from pydantic import BaseModel

from core.registries import get_agent_registry
from core.state import VentureState
from workflows.deployment_pipeline import compile_deployment_pipeline

logger = logging.getLogger("afos.agents.deployment_pipeline")

get_agent_registry().register_from_yaml(Path(__file__).parent / "manifest.yaml")

_VALID_DEPTHS = {"standard", "deep"}


class InfraStageResult(BaseModel):
    stage: str
    healthy: bool = False
    prepared: bool = False
    details: dict[str, Any] = {}
    error: str = ""


class DeploymentReport(BaseModel):
    """The Deployment Pipeline's single output contract - a summary of the
    Docker/PostgreSQL/Chroma/Ollama/n8n preparation run assembled entirely from
    the already-completed AI Builder Pipeline's Build Report and the reused
    Phase 3 Infrastructure Agents' own results.
    """

    idea: str
    status: str = "pending"
    build_validated: bool = False
    stages: list[InfraStageResult] = []
    ready_stage_count: int = 0
    total_stage_count: int = 0
    summary: str = ""
    error: str = ""


def run_deployment_pipeline(idea: str, venture_id: str, research_depth: str = "standard") -> dict:
    """Entry point for running the Deployment Pipeline. Registered in the
    existing Agent Registry above; declares no permissions and calls the Tool
    Registry for nothing directly - it only ever reaches outside itself through
    the already-completed AI Builder Pipeline (Phase 4 Component 5) and the
    existing, unmodified Phase 3 Infrastructure Agents it orchestrates (see
    workflows/deployment_pipeline.py). Never raises for a genuine failure
    (GraphBubbleUp aside) - invalid input, a failed/unsuccessful build, and a
    partially/fully unreachable infrastructure stack all degrade to a
    well-formed DeploymentReport with a descriptive status.

    Returns a dict shaped {"agent": ..., "event": ..., **DeploymentReport
    fields}.
    """
    depth = research_depth if research_depth in _VALID_DEPTHS else "standard"

    initial_state: dict[str, Any] = {
        "idea": idea,
        "venture_id": venture_id,
        "phase": "launch",
        "research_depth": depth,
    }

    try:
        graph = compile_deployment_pipeline()
        result = graph.invoke(initial_state)
        report = DeploymentReport(**result.get("deployment_report", {"idea": idea, "status": "failed"}))
        return {"agent": "deployment_pipeline", "event": "deployment_pipeline_result", **report.model_dump()}
    except GraphBubbleUp:
        raise
    except Exception as exc:
        logger.warning("deployment_pipeline: unexpected pipeline failure for venture '%s': %s", venture_id, exc)
        return {
            "agent": "deployment_pipeline",
            "event": "deployment_pipeline_result",
            **DeploymentReport(idea=idea, status="failed", error=str(exc)).model_dump(),
        }


def deployment_pipeline_node(state: VentureState) -> dict:
    """Manager-callable entry point in the same node shape every other agent uses.
    Not wired into any graph in this component. Uses idea/venture_id already
    present on VentureState - no extra state fields required.
    """
    entry = run_deployment_pipeline(idea=state.get("idea", ""), venture_id=state.get("venture_id", "default"))
    return {"history": [entry]}
