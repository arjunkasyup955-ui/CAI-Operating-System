import logging
from pathlib import Path
from typing import Any

from langgraph.errors import GraphBubbleUp
from langgraph.types import Command
from pydantic import BaseModel

from core.registries import get_agent_registry  # noqa: I001 (must precede core.permissions - see note below)
from core.memory_gateway import get_memory_gateway
from core.permissions import Permission, get_permission_gate
from core.state import VentureState
import workflows.founder_pipeline as founder_pipeline
from workflows.founder_pipeline import compile_founder_pipeline

# core.registries must be imported before core.permissions - a pre-existing kernel
# import-order fragility (core.registries.tool_registry imports
# core.permissions.PermissionDeniedError at module load time, so importing
# core.permissions first triggers a circular partial-init). Documented, not fixed:
# core/ is frozen and this ordering constraint predates this component.

logger = logging.getLogger("afos.agents.founder_orchestrator")

get_agent_registry().register_from_yaml(Path(__file__).parent / "manifest.yaml")


class FounderExecutionPlan(BaseModel):
    """The Founder Orchestrator's single output contract: Idea -> Research ->
    Analysis -> Decision, structured into a plan the rest of AFOS (or a human) can
    act on.
    """

    idea: str
    venture_id: str
    constraints: dict[str, Any] = {}
    budget: float | None = None
    target_market: str = ""
    research_tasks: list[str] = []
    analysis_tasks: list[str] = []
    decision_tasks: list[str] = []
    required_agents: list[str] = []
    execution_order: list[str] = []
    estimated_runtime_seconds: float = 0.0
    risk_level: str = "high"
    status: str = "pending"
    summary: str = ""
    error: str = ""


def _thread_id(venture_id: str) -> str:
    return f"founder-{venture_id}"


def run_founder_pipeline(
    idea: str,
    venture_id: str,
    constraints: dict[str, Any] | None = None,
    budget: float | None = None,
    target_market: str = "",
) -> dict:
    """Entry point for kicking off a founder pipeline run. Every call is gated by
    the existing Permission Layer (a self-check against this agent's own granted
    permissions - defensive, since the manifest already declares internet_access,
    but real wiring through PermissionGate rather than a no-op), and every pipeline
    run flows through the existing Approval Framework, Event Bus, and Memory
    Gateway (the LangGraph checkpointer backing pause/resume) inside
    workflows/founder_pipeline.py. Never bypasses the Tool Registry: this
    orchestrator calls no external system directly - it only coordinates the
    already-registered, already-tool-backed Research Supervisor and its 7 workers.

    Returns a dict shaped {"agent": ..., "event": ..., **FounderExecutionPlan
    fields} on completion/rejection/failure, or {"agent": ..., "event":
    "founder_pipeline_pending_approval", "interrupt": ...} if a high-budget run
    paused for human approval - call resume_founder_pipeline() to continue it.
    """
    manifest = get_agent_registry().get("founder_orchestrator")
    get_permission_gate().check(manifest, Permission.INTERNET_ACCESS)

    initial_state: dict[str, Any] = {
        "idea": idea,
        "venture_id": venture_id,
        "phase": "idea",
        "constraints": constraints or {},
        "budget": budget,
        "target_market": target_market,
    }

    try:
        gateway = get_memory_gateway()
        with gateway.working() as checkpointer:
            graph = compile_founder_pipeline(checkpointer)
            config = {"configurable": {"thread_id": _thread_id(venture_id)}}
            result = graph.invoke(initial_state, config=config)

            if "__interrupt__" in result:
                return {
                    "agent": "founder_orchestrator",
                    "event": "founder_pipeline_pending_approval",
                    "interrupt": result["__interrupt__"][0].value,
                }

            plan = FounderExecutionPlan(**result.get("execution_plan", {"idea": idea, "venture_id": venture_id}))
            return {"agent": "founder_orchestrator", "event": "founder_pipeline_result", **plan.model_dump()}
    except GraphBubbleUp:
        raise
    except Exception as exc:
        # Belt-and-suspenders: every graph node already catches its own
        # exceptions (research_node most notably), but a genuinely unexpected
        # failure anywhere in the graph must still degrade gracefully here rather
        # than propagate - and must release the duplicate-execution guard, or a
        # bug elsewhere would permanently wedge this venture_id.
        logger.warning("founder_orchestrator: unexpected pipeline failure for venture '%s': %s", venture_id, exc)
        founder_pipeline._in_progress.discard(venture_id)
        return {
            "agent": "founder_orchestrator",
            "event": "founder_pipeline_result",
            **FounderExecutionPlan(idea=idea, venture_id=venture_id, status="failed", error=str(exc)).model_dump(),
        }


def resume_founder_pipeline(venture_id: str, approved: bool, reason: str = "") -> dict:
    """Resumes a founder pipeline run that paused for human approval (see
    run_founder_pipeline's high-budget path), using the same persistent
    Memory-Gateway-backed thread_id.
    """
    try:
        gateway = get_memory_gateway()
        with gateway.working() as checkpointer:
            graph = compile_founder_pipeline(checkpointer)
            config = {"configurable": {"thread_id": _thread_id(venture_id)}}
            result = graph.invoke(Command(resume={"approved": approved, "reason": reason}), config=config)

            if "__interrupt__" in result:
                return {
                    "agent": "founder_orchestrator",
                    "event": "founder_pipeline_pending_approval",
                    "interrupt": result["__interrupt__"][0].value,
                }

            plan = FounderExecutionPlan(**result.get("execution_plan", {"idea": "", "venture_id": venture_id}))
            return {"agent": "founder_orchestrator", "event": "founder_pipeline_result", **plan.model_dump()}
    except GraphBubbleUp:
        raise
    except Exception as exc:
        logger.warning("founder_orchestrator: unexpected resume failure for venture '%s': %s", venture_id, exc)
        founder_pipeline._in_progress.discard(venture_id)
        return {
            "agent": "founder_orchestrator",
            "event": "founder_pipeline_result",
            **FounderExecutionPlan(idea="", venture_id=venture_id, status="failed", error=str(exc)).model_dump(),
        }


def founder_orchestrator_node(state: VentureState) -> dict:
    """Manager-callable entry point in the same node shape every other agent uses.
    Not wired into any graph in this component. Uses idea/venture_id already
    present on VentureState - no extra state fields required.
    """
    entry = run_founder_pipeline(idea=state.get("idea", ""), venture_id=state.get("venture_id", "default"))
    return {"history": [entry]}
