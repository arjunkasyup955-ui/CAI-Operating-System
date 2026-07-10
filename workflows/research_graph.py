from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agents.research.search.agent import search_node
from agents.research.supervisor.agent import research_intake_node
from core.state import VentureState


def build_research_graph() -> StateGraph:
    """Research Supervisor subgraph. Component 2 adds the Search Agent as the first
    real worker - Browser/Market Intelligence/Competitor Analysis/Trend Detection/
    Opportunity Detection/Idea Validation workers each land here as their own
    component, extending this same internal graph. The Supervisor's external contract
    (its manifest, and how main_graph.py composes it) is unchanged.
    """
    graph = StateGraph(VentureState)
    graph.add_node("research_intake", research_intake_node)
    graph.add_node("search", search_node)
    graph.add_edge(START, "research_intake")
    graph.add_edge("research_intake", "search")
    graph.add_edge("search", END)
    return graph


def compile_research_graph() -> CompiledStateGraph:
    # Deliberately compiled without a checkpointer - see main_graph.py's
    # research_supervisor_node for why this subgraph is invoked statelessly.
    return build_research_graph().compile()
