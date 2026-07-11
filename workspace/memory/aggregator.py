from typing import Any

from workspace.memory.store import MemoryKind, get_default_project_memory_store


def record_research_memory(venture_id: str, data: dict[str, Any], source: str = "research_pipeline") -> dict[str, Any]:
    return get_default_project_memory_store().record(MemoryKind.RESEARCH, venture_id, data, source=source)


def get_research_memory(venture_id: str) -> list[dict[str, Any]]:
    return get_default_project_memory_store().list_entries(MemoryKind.RESEARCH, venture_id)


def record_competitor_memory(venture_id: str, data: dict[str, Any], source: str = "competitor_intelligence") -> dict[str, Any]:
    return get_default_project_memory_store().record(MemoryKind.COMPETITOR, venture_id, data, source=source)


def get_competitor_memory(venture_id: str) -> list[dict[str, Any]]:
    return get_default_project_memory_store().list_entries(MemoryKind.COMPETITOR, venture_id)


def record_decision_memory(venture_id: str, data: dict[str, Any], source: str = "decision_engine") -> dict[str, Any]:
    return get_default_project_memory_store().record(MemoryKind.DECISION, venture_id, data, source=source)


def get_decision_memory(venture_id: str) -> list[dict[str, Any]]:
    return get_default_project_memory_store().list_entries(MemoryKind.DECISION, venture_id)


def record_mvp_memory(venture_id: str, data: dict[str, Any], source: str = "mvp_planner") -> dict[str, Any]:
    return get_default_project_memory_store().record(MemoryKind.MVP, venture_id, data, source=source)


def get_mvp_memory(venture_id: str) -> list[dict[str, Any]]:
    return get_default_project_memory_store().list_entries(MemoryKind.MVP, venture_id)


def record_builder_memory(venture_id: str, data: dict[str, Any], source: str = "ai_builder") -> dict[str, Any]:
    return get_default_project_memory_store().record(MemoryKind.BUILDER, venture_id, data, source=source)


def get_builder_memory(venture_id: str) -> list[dict[str, Any]]:
    return get_default_project_memory_store().list_entries(MemoryKind.BUILDER, venture_id)


# ------------------------------------------------------------------------ #
# Read-through wrappers over Phase 5 Components 4/6/7's own already-public
# stores - "Deployment Memory", "Approval History", and "Execution History"
# are never duplicated into a second store here; these functions are the
# entire implementation, calling only public get_default_*_store() APIs
# those components already export.
# ------------------------------------------------------------------------ #

def get_deployment_memory(venture_id: str) -> list[dict[str, Any]]:
    from deployment.history import get_default_deployment_history_store

    return get_default_deployment_history_store().list_deployments(venture_id=venture_id)


def get_approval_history(venture_id: str) -> list[dict[str, Any]]:
    from agents.founder.human_approval.agent import get_default_approval_store

    return get_default_approval_store().list_requests(venture_id=venture_id)


def get_execution_history(venture_id: str) -> list[dict[str, Any]]:
    from core.observability import get_default_execution_history

    return get_default_execution_history().list_executions(venture_id=venture_id)


def get_all_project_memory(venture_id: str) -> dict[str, Any]:
    """One-call aggregation across all 8 memory kinds - the literal
    "Persistent Project Memory" contract: Research, Competitor, Decision,
    MVP, Builder, Deployment, Approval History, Execution History.
    """
    return {
        "research": get_research_memory(venture_id),
        "competitor": get_competitor_memory(venture_id),
        "decision": get_decision_memory(venture_id),
        "mvp": get_mvp_memory(venture_id),
        "builder": get_builder_memory(venture_id),
        "deployment": get_deployment_memory(venture_id),
        "approval_history": get_approval_history(venture_id),
        "execution_history": get_execution_history(venture_id),
    }
