import os
from typing import Any, Protocol

from dotenv import load_dotenv
from pydantic import BaseModel

# Deliberately self-contained (own env reading, not core.config.Settings) so this tool
# module never needs the frozen Phase 0 kernel to change to gain a new config key -
# same convention as tools/web, tools/browser, tools/ollama, tools/database.
load_dotenv()


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


class ChromaHealthStatus(BaseModel):
    healthy: bool
    backend: str
    error: str = ""


class ChromaDocument(BaseModel):
    id: str
    content: str
    metadata: dict[str, Any] = {}
    distance: float | None = None


class ChromaOperationResult(BaseModel):
    success: bool
    operation: str
    documents: list[ChromaDocument] = []
    count: int = 0
    error: str = ""


class EmbeddingProvider(Protocol):
    """Separate from ChromaProvider itself - embedding generation and vector storage
    are distinct concerns, and this abstraction is what lets the embedding backend be
    swapped (or faked in tests) independently of the vector store backend.
    """

    name: str

    def embed(self, text: str) -> list[float]: ...


class OpenAIEmbeddingProvider:
    """Reuses the Memory Gateway's existing embedding call (core/memory_gateway/
    vector_store.py::_openai_embed) instead of re-implementing an OpenAI embeddings
    call from scratch - per the explicit "do not duplicate vector-store logic already
    present in the kernel" requirement. The kernel's vector tier and this Chroma
    integration end up using the exact same embedding model/dimensionality.
    """

    name = "openai"

    def embed(self, text: str) -> list[float]:
        from core.memory_gateway.vector_store import _openai_embed

        return _openai_embed(text)


class ChromaProvider(Protocol):
    """Collection lifecycle + document CRUD + similarity search. Every method either
    returns a *Result on success or raises on failure - the agent layer's try/except
    (with ToolRegistry's RetryPolicy) handles graceful degradation uniformly, the same
    pattern as every prior provider.
    """

    name: str

    def health_check(self) -> ChromaHealthStatus: ...
    def create_collection(self, name: str) -> ChromaOperationResult: ...
    def delete_collection(self, name: str) -> ChromaOperationResult: ...
    def list_collections(self) -> ChromaOperationResult: ...
    def add_documents(self, collection: str, documents: list[dict[str, Any]]) -> ChromaOperationResult: ...
    def update_documents(self, collection: str, documents: list[dict[str, Any]]) -> ChromaOperationResult: ...
    def delete_documents(self, collection: str, ids: list[str]) -> ChromaOperationResult: ...
    def similarity_search(
        self, collection: str, query: str, top_k: int = 5, where: dict[str, Any] | None = None
    ) -> ChromaOperationResult: ...


class ChromaDBProvider:
    """Default ChromaProvider - real, persistent-local Chroma via the `chromadb`
    package. Lazily imported so this module loads fine even when chromadb isn't
    installed (it isn't, in this sandbox - no server process needed either way,
    PersistentClient is embedded/file-based); a call then raises a clear, actionable
    error, exactly the same pattern as PlaywrightProvider/Crawl4AIProvider and
    PsycopgPostgresProvider.
    """

    name = "chromadb"

    def __init__(self, persist_directory: str | None = None, embedding_provider: EmbeddingProvider | None = None) -> None:
        self._persist_directory = persist_directory or _env("CHROMA_PERSIST_DIR", "database/chroma")
        self._embedding_provider = embedding_provider or OpenAIEmbeddingProvider()
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import chromadb
            except ImportError as exc:
                raise RuntimeError("chromadb is not installed - `pip install chromadb`") from exc
            self._client = chromadb.PersistentClient(path=self._persist_directory)
        return self._client

    def health_check(self) -> ChromaHealthStatus:
        try:
            client = self._get_client()
            client.heartbeat()
            return ChromaHealthStatus(healthy=True, backend="persistent")
        except Exception as exc:
            return ChromaHealthStatus(healthy=False, backend="persistent", error=str(exc))

    def create_collection(self, name: str) -> ChromaOperationResult:
        self._get_client().get_or_create_collection(name)
        return ChromaOperationResult(success=True, operation="create_collection", count=0)

    def delete_collection(self, name: str) -> ChromaOperationResult:
        self._get_client().delete_collection(name)
        return ChromaOperationResult(success=True, operation="delete_collection", count=0)

    def list_collections(self) -> ChromaOperationResult:
        names = [c.name for c in self._get_client().list_collections()]
        docs = [ChromaDocument(id=n, content="") for n in names]
        return ChromaOperationResult(success=True, operation="list_collections", documents=docs, count=len(names))

    def add_documents(self, collection: str, documents: list[dict[str, Any]]) -> ChromaOperationResult:
        col = self._get_client().get_or_create_collection(collection)
        ids = [d["id"] for d in documents]
        contents = [d["content"] for d in documents]
        metadatas = [d.get("metadata", {}) for d in documents]
        embeddings = [self._embedding_provider.embed(c) for c in contents]
        col.add(ids=ids, documents=contents, metadatas=metadatas, embeddings=embeddings)
        return ChromaOperationResult(success=True, operation="add_documents", count=len(ids))

    def update_documents(self, collection: str, documents: list[dict[str, Any]]) -> ChromaOperationResult:
        col = self._get_client().get_or_create_collection(collection)
        ids = [d["id"] for d in documents]
        contents = [d["content"] for d in documents]
        metadatas = [d.get("metadata", {}) for d in documents]
        embeddings = [self._embedding_provider.embed(c) for c in contents]
        col.update(ids=ids, documents=contents, metadatas=metadatas, embeddings=embeddings)
        return ChromaOperationResult(success=True, operation="update_documents", count=len(ids))

    def delete_documents(self, collection: str, ids: list[str]) -> ChromaOperationResult:
        col = self._get_client().get_or_create_collection(collection)
        col.delete(ids=ids)
        return ChromaOperationResult(success=True, operation="delete_documents", count=len(ids))

    def similarity_search(
        self, collection: str, query: str, top_k: int = 5, where: dict[str, Any] | None = None
    ) -> ChromaOperationResult:
        col = self._get_client().get_or_create_collection(collection)
        query_embedding = self._embedding_provider.embed(query)
        results = col.query(query_embeddings=[query_embedding], n_results=top_k, where=where)
        ids = results["ids"][0]
        contents = results["documents"][0]
        metadatas = results["metadatas"][0]
        distances = results["distances"][0]
        docs = [
            ChromaDocument(id=ids[i], content=contents[i], metadata=metadatas[i] or {}, distance=distances[i])
            for i in range(len(ids))
        ]
        return ChromaOperationResult(success=True, operation="similarity_search", documents=docs, count=len(docs))


class FakeChromaProvider:
    """In-memory ChromaProvider - deterministic, no real network/embedding-API/server
    needed. Similarity is computed via word-overlap (Jaccard) rather than real vector
    distance, so results are fully predictable for tests without depending on a live
    embedding call. Metadata filtering is exact-match, mirroring Chroma's own `where`
    equality semantics for the common case.
    """

    name = "fake_chroma"

    def __init__(self) -> None:
        self._collections: dict[str, dict[str, dict[str, Any]]] = {}

    def health_check(self) -> ChromaHealthStatus:
        return ChromaHealthStatus(healthy=True, backend="in_memory")

    def create_collection(self, name: str) -> ChromaOperationResult:
        self._collections.setdefault(name, {})
        return ChromaOperationResult(success=True, operation="create_collection", count=0)

    def delete_collection(self, name: str) -> ChromaOperationResult:
        if name not in self._collections:
            raise ValueError(f"collection '{name}' does not exist")
        del self._collections[name]
        return ChromaOperationResult(success=True, operation="delete_collection", count=0)

    def list_collections(self) -> ChromaOperationResult:
        names = list(self._collections.keys())
        docs = [ChromaDocument(id=n, content="") for n in names]
        return ChromaOperationResult(success=True, operation="list_collections", documents=docs, count=len(names))

    def add_documents(self, collection: str, documents: list[dict[str, Any]]) -> ChromaOperationResult:
        col = self._collections.setdefault(collection, {})
        for doc in documents:
            col[doc["id"]] = {"content": doc["content"], "metadata": doc.get("metadata", {})}
        return ChromaOperationResult(success=True, operation="add_documents", count=len(documents))

    def update_documents(self, collection: str, documents: list[dict[str, Any]]) -> ChromaOperationResult:
        col = self._collections.setdefault(collection, {})
        for doc in documents:
            if doc["id"] not in col:
                raise ValueError(f"document '{doc['id']}' does not exist in collection '{collection}'")
            col[doc["id"]] = {"content": doc["content"], "metadata": doc.get("metadata", {})}
        return ChromaOperationResult(success=True, operation="update_documents", count=len(documents))

    def delete_documents(self, collection: str, ids: list[str]) -> ChromaOperationResult:
        col = self._collections.get(collection, {})
        deleted = 0
        for doc_id in ids:
            if doc_id in col:
                del col[doc_id]
                deleted += 1
        return ChromaOperationResult(success=True, operation="delete_documents", count=deleted)

    def similarity_search(
        self, collection: str, query: str, top_k: int = 5, where: dict[str, Any] | None = None
    ) -> ChromaOperationResult:
        col = self._collections.get(collection, {})
        query_words = set(query.lower().split())
        scored: list[tuple[float, str, dict[str, Any]]] = []
        for doc_id, doc in col.items():
            if where and not all(doc["metadata"].get(k) == v for k, v in where.items()):
                continue
            doc_words = set(doc["content"].lower().split())
            union = len(query_words | doc_words) or 1
            overlap = len(query_words & doc_words)
            distance = 1.0 - (overlap / union)
            scored.append((distance, doc_id, doc))
        scored.sort(key=lambda x: x[0])
        top = scored[:top_k]
        docs = [ChromaDocument(id=doc_id, content=doc["content"], metadata=doc["metadata"], distance=dist) for dist, doc_id, doc in top]
        return ChromaOperationResult(success=True, operation="similarity_search", documents=docs, count=len(docs))


_provider: ChromaProvider = ChromaDBProvider()


def get_chroma_provider() -> ChromaProvider:
    return _provider


def set_chroma_provider(provider: ChromaProvider) -> None:
    """Swappable, not cached - lets tests inject a deterministic fake provider."""
    global _provider
    _provider = provider
