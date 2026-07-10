from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agents.research.supervisor.agent import research_intake_node
from core.state import VentureState


def build_research_graph() -> StateGraph:
    """Research Supervisor subgraph. Component 1 wires only the intake stub node -
    Search/Browser/Market Intelligence/Competitor Analysis/Trend Detection/Opportunity
    Detection/Idea Validation workers each land here as their own component.
    """
    graph = StateGraph(VentureState)
    graph.add_node("research_intake", research_intake_node)
    graph.add_edge(START, "research_intake")
    graph.add_edge("research_intake", END)
    return graph


def compile_research_graph() -> CompiledStateGraph:
    # Deliberately compiled without a checkpointer - see main_graph.py's
    # research_supervisor_node for why this subgraph is invoked statelessly.
    return build_research_graph().compile()
