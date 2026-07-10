import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class AFOSEvent(BaseModel):
    """The one shape every cross-domain notification takes. Published for things that
    don't fit a parent -> child call: scheduler triggers, budget alerts, plugin hooks,
    cross-supervisor notifications. Intra-workflow steps stay LangGraph edges, not events.
    """

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: str
    source_agent: str
    venture_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
