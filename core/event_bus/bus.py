import logging
from collections import defaultdict
from collections.abc import Callable
from functools import lru_cache

from core.event_bus.events import AFOSEvent

logger = logging.getLogger("afos.event_bus")

Handler = Callable[[AFOSEvent], None]


class EventBus:
    """In-process pub/sub. Start here - no new infra. Upgrade to Redis Streams/NATS only
    once execution is genuinely multi-process/multi-machine, not before.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Handler]] = defaultdict(list)
        self._history: list[AFOSEvent] = []

    def subscribe(self, event_type: str, handler: Handler) -> None:
        """event_type may be a concrete type (e.g. "research.completed") or "*" for all events."""
        self._subscribers[event_type].append(handler)

    def publish(self, event: AFOSEvent) -> None:
        self._history.append(event)
        logger.info("event published: %s from %s", event.type, event.source_agent)
        for handler in self._subscribers.get(event.type, []):
            handler(event)
        for handler in self._subscribers.get("*", []):
            handler(event)

    def history(self, event_type: str | None = None) -> list[AFOSEvent]:
        if event_type is None:
            return list(self._history)
        return [e for e in self._history if e.type == event_type]


@lru_cache
def get_event_bus() -> EventBus:
    return EventBus()
