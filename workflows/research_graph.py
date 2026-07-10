from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agents.research.browser.agent import browser_node
from agents.research.competitor_intelligence.agent import competitor_intelligence_node
from agents.research.idea_validation.agent import idea_validation_node
from agents.research.market_intelligence.agent import market_intelligence_node
from agents.research.opportunity_detection.agent import opportunity_detection_node
from agents.research.search.agent import search_node
from agents.research.supervisor.agent import research_intake_node
from agents.research.trend_intelligence.agent import trend_intelligence_node
from core.state import VentureState


def build_research_graph() -> StateGraph:
    """Research Supervisor subgraph. Component 8 adds the Idea Validation Agent after
    Opportunity Detection - the final worker for Phase 1's research/validation chain.
    The Supervisor's external contract (its manifest, and how main_graph.py composes
    it) is unchanged.
    """
    graph = StateGraph(VentureState)
    graph.add_node("research_intake", research_intake_node)
    graph.add_node("search", search_node)
    graph.add_node("browser", browser_node)
    graph.add_node("market_intelligence", market_intelligence_node)
    graph.add_node("competitor_intelligence", competitor_intelligence_node)
    graph.add_node("trend_intelligence", trend_intelligence_node)
    graph.add_node("opportunity_detection", opportunity_detection_node)
    graph.add_node("idea_validation", idea_validation_node)
    graph.add_edge(START, "research_intake")
    graph.add_edge("research_intake", "search")
    graph.add_edge("search", "browser")
    graph.add_edge("browser", "market_intelligence")
    graph.add_edge("market_intelligence", "competitor_intelligence")
    graph.add_edge("competitor_intelligence", "trend_intelligence")
    graph.add_edge("trend_intelligence", "opportunity_detection")
    graph.add_edge("opportunity_detection", "idea_validation")
    graph.add_edge("idea_validation", END)
    return graph


def compile_research_graph() -> CompiledStateGraph:
    # Deliberately compiled without a checkpointer - see main_graph.py's
    # research_supervisor_node for why this subgraph is invoked statelessly.
    return build_research_graph().compile()
