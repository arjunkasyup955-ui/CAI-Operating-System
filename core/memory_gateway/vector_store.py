import json
import sqlite3
from collections.abc import Callable

import sqlite_vec

from core.config import get_settings

EMBEDDING_DIM = 1536
EMBEDDING_MODEL = "text-embedding-3-small"


def _openai_embed(text: str) -> list[float]:
    from openai import OpenAI

    client = OpenAI(api_key=get_settings().openai_api_key)
    response = client.embeddings.create(model=EMBEDDING_MODEL, input=text)
    return list(response.data[0].embedding)


class VectorStore:
    """SQLite + the sqlite-vec extension for cosine/L2 similarity search. This is the
    interim implementation for the vector tier - swap for Chroma/LanceDB (or a managed
    service) once semantic search volume actually justifies new infra, not before.
    """

    def __init__(self, conn: sqlite3.Connection, embed_fn: Callable[[str], list[float]] | None = None) -> None:
        self._conn = conn
        self._embed_fn = embed_fn or _openai_embed
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.enable_load_extension(True)
        sqlite_vec.load(self._conn)
        self._conn.enable_load_extension(False)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS vector_documents (
                id INTEGER PRIMARY KEY,
                venture_id TEXT,
                kind TEXT,
                text TEXT,
                metadata TEXT
            )"""
        )
        self._conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vector_embeddings USING vec0(embedding float[{EMBEDDING_DIM}])"
        )
        self._conn.commit()

    def add(self, text: str, venture_id: str | None = None, kind: str = "note", metadata: dict | None = None) -> int:
        cur = self._conn.execute(
            "INSERT INTO vector_documents (venture_id, kind, text, metadata) VALUES (?, ?, ?, ?)",
            (venture_id, kind, text, json.dumps(metadata or {})),
        )
        doc_id = cur.lastrowid
        embedding = self._embed_fn(text)
        self._conn.execute(
            "INSERT INTO vector_embeddings (rowid, embedding) VALUES (?, ?)",
            (doc_id, sqlite_vec.serialize_float32(embedding)),
        )
        self._conn.commit()
        return doc_id

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        query_embedding = self._embed_fn(query)
        rows = self._conn.execute(
            """
            SELECT vd.id, vd.text, vd.kind, vd.venture_id, vd.metadata, ve.distance
            FROM vector_embeddings ve
            JOIN vector_documents vd ON vd.id = ve.rowid
            WHERE ve.embedding MATCH ? AND k = ?
            ORDER BY ve.distance
            """,
            (sqlite_vec.serialize_float32(query_embedding), top_k),
        ).fetchall()
        return [
            {
                "id": r[0],
                "text": r[1],
                "kind": r[2],
                "venture_id": r[3],
                "metadata": json.loads(r[4]),
                "distance": r[5],
            }
            for r in rows
        ]
