import sqlite3
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from core.config import get_settings
from core.memory_gateway.knowledge_schema import init_knowledge_schema
from core.memory_gateway.vector_store import VectorStore


class MemoryGateway:
    """The only way agents touch storage - four tiers behind one interface. No agent
    imports sqlite3 / a vector lib / a graph lib directly.

    - working:       LangGraph checkpointer (thread/run state)
    - vector:         semantic recall over research & reports
    - knowledge_conn: entity/relationship tables (schema only in Phase 0)
    - long-term (structured business entities) arrives with the Research/Product agents
      in later phases - Phase 0 has no domain entities to store yet.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        self._store_conn = sqlite3.connect(db_path, check_same_thread=False)
        init_knowledge_schema(self._store_conn)
        self.vector = VectorStore(self._store_conn)

    @contextmanager
    def working(self):
        with SqliteSaver.from_conn_string(self._db_path) as saver:
            yield saver

    @property
    def knowledge_conn(self) -> sqlite3.Connection:
        return self._store_conn


@lru_cache
def get_memory_gateway() -> MemoryGateway:
    return MemoryGateway(get_settings().afos_db_path)
