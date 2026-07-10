from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import RetryPolicy

from agents.manager.agent import approval_gate_node, manager_node
from core.state import VentureState
from workflows.research_graph import compile_research_graph

# Nodes that call out to LLMs/tools should retry on transient failures at the graph
# level, not just inside ToolRegistry - a node can fail before ever reaching a tool call.
_NODE_RETRY_POLICY = RetryPolicy(max_attempts=3, initial_interval=0.1, backoff_factor=2.0)

_research_graph = compile_research_graph()


def research_supervisor_node(state: VentureState) -> dict:
    """Wraps the Research Supervisor subgraph as a plain node function rather than
    composing the compiled subgraph directly via add_node(). Composing it directly
    causes silent double-accumulation of Annotated[list, operator.add] fields (like
    history/research_findings): the subgraph inherits the parent's live channel value
    as its own starting state, so its returned (already-combined) result gets added a
    second time by the parent's own reducer on the way out - confirmed empirically.

    The fix: invoke the subgraph statelessly (no shared config/thread_id, so it never
    sees the parent's already-accumulated lists) with a scoped input, then return only
    the subgraph's own new items as this node's delta.
    """
    child_result = _research_graph.invoke(
        {"idea": state.get("idea", ""), "venture_id": state.get("venture_id", ""), "phase": state.get("phase")}
    )
    return {
        "research_findings": child_result.get("research_findings", []),
        "history": child_result.get("history", []),
    }


def _route_after_approval(state: VentureState) -> str:
    """In Phase 0, approval_gate only ever led to END, so a rejected decision had
    nowhere to wrongly proceed to. Phase 1 adds a real next step, which makes that
    latent gap an actual bug: without this check, research would run even after a
    human rejects the gate. Route to research_supervisor only on an approved decision.
    """
    for entry in reversed(state.get("history", [])):
        if entry.get("event") == "approval_decision":
            return "research_supervisor" if entry.get("approved") else END
    return END


def build_main_graph() -> StateGraph:
    """Top-level Manager graph. Research Supervisor is wired in as of Phase 1 Component
    1 (stub intake worker only). Product/Growth/Revenue/Ops Supervisors plug in the
    same way as their own components land.
    """
    graph = StateGraph(VentureState)
    graph.add_node("manager", manager_node, retry_policy=_NODE_RETRY_POLICY)
    graph.add_node("approval_gate", approval_gate_node)
    graph.add_node("research_supervisor", research_supervisor_node, retry_policy=_NODE_RETRY_POLICY)
    graph.add_edge(START, "manager")
    graph.add_edge("manager", "approval_gate")
    graph.add_conditional_edges("approval_gate", _route_after_approval, ["research_supervisor", END])
    graph.add_edge("research_supervisor", END)
    return graph


def compile_main_graph(checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:
    return build_main_graph().compile(checkpointer=checkpointer)
