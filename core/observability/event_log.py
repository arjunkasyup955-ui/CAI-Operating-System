from typing import Any

from core.event_bus import get_event_bus


def get_pipeline_events(
    venture_id: str | None = None,
    event_type: str | None = None,
    search: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Reads directly from the Event Bus's own history() (Phase 0, unmodified)
    - no separate event capture is built here, since the Event Bus already
    durably records every AFOSEvent ever published for the process's
    lifetime. This function only adds dashboard-facing filtering/pagination
    on top of that existing, real data.
    """
    events = get_event_bus().history(event_type)
    items = [e.model_dump(mode="json") for e in reversed(events)]
    if venture_id is not None:
        items = [e for e in items if e.get("venture_id") == venture_id]
    if search:
        needle = search.lower()
        items = [e for e in items if needle in e.get("type", "").lower() or needle in e.get("source_agent", "").lower()]
    items = items[offset:]
    if limit is not None:
        items = items[:limit]
    return items
