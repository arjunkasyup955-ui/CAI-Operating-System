import logging
from pathlib import Path

from core.registries import get_agent_registry
from core.state import VentureState

logger = logging.getLogger("afos.agents.research_supervisor")

get_agent_registry().register_from_yaml(Path(__file__).parent / "manifest.yaml")


def research_intake_node(state: VentureState) -> dict:
    """Phase 1 Component 1 stub: proves the Research Supervisor subgraph runs and
    returns state correctly. No external search/browser/LLM calls yet - those arrive
    as their own components (Search Agent, Browser Agent, ...).
    """
    idea = state.get("idea", "")
    logger.info("research supervisor received idea: %s", idea)
    return {
        "research_findings": [{"agent": "research_supervisor", "event": "intake", "idea": idea}],
        "history": [{"agent": "research_supervisor", "event": "intake", "idea": idea}],
    }
