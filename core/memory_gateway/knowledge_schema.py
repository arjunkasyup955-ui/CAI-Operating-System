import sqlite3

# Knowledge tier: entities + typed relationships connecting Ideas, Companies, Products,
# Competitors, Technologies, Markets, Customers, Reports, ResearchFindings. Phase 0 only
# creates this schema - traversal/query API is a Phase 1 deliverable, once Research agents
# are actually populating it.

_DDL = """
CREATE TABLE IF NOT EXISTS kg_entities (
    id INTEGER PRIMARY KEY,
    venture_id TEXT,
    entity_type TEXT NOT NULL,
    name TEXT NOT NULL,
    attributes TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS kg_relationships (
    id INTEGER PRIMARY KEY,
    from_entity_id INTEGER NOT NULL REFERENCES kg_entities(id),
    to_entity_id INTEGER NOT NULL REFERENCES kg_entities(id),
    relationship_type TEXT NOT NULL,
    attributes TEXT NOT NULL DEFAULT '{}'
);
"""


def init_knowledge_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    conn.commit()
