from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import RetryPolicy

from agents.manager.agent import approval_gate_node, manager_node
from core.state import VentureState

# Nodes that call out to LLMs/tools should retry on transient failures at the graph
# level, not just inside ToolRegistry - a node can fail before ever reaching a tool call.
_NODE_RETRY_POLICY = RetryPolicy(max_attempts=3, initial_interval=0.1, backoff_factor=2.0)


def build_main_graph() -> StateGraph:
    """Top-level Manager graph. Domain Supervisor subgraphs (research/product/growth/
    revenue/ops) plug in here starting Phase 1 - Phase 0 only proves the seam with a
    Manager stub and one real approval gate.
    """
    graph = StateGraph(VentureState)
    graph.add_node("manager", manager_node, retry_policy=_NODE_RETRY_POLICY)
    graph.add_node("approval_gate", approval_gate_node)
    graph.add_edge(START, "manager")
    graph.add_edge("manager", "approval_gate")
    graph.add_edge("approval_gate", END)
    return graph


def compile_main_graph(checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:
    return build_main_graph().compile(checkpointer=checkpointer)
