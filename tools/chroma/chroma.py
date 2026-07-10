from typing import Any

from pydantic import BaseModel, Field

from core.registries import ToolSpec, get_tool_registry
from core.registries.tool_registry import RetryPolicy
from tools.chroma.chroma_providers import get_chroma_provider

_RETRY_POLICY = RetryPolicy(max_attempts=2, backoff_seconds=1.0)
_TIMEOUT_SECONDS = 20.0


class HealthCheckArgs(BaseModel):
    pass


class CreateCollectionArgs(BaseModel):
    # Deliberately "collection_name", not "name" - ToolRegistry.invoke()'s own
    # positional parameter is literally called `name` (the tool's name), so a field
    # called `name` passed through **kwargs collides with it ("got multiple values
    # for argument 'name'"). This bug was found and fixed here; it's a real, generic
    # risk worth checking for in any tool schema.
    collection_name: str


class DeleteCollectionArgs(BaseModel):
    collection_name: str


class ListCollectionsArgs(BaseModel):
    pass


class AddDocumentsArgs(BaseModel):
    collection: str
    documents: list[dict[str, Any]] = Field(min_length=1)


class UpdateDocumentsArgs(BaseModel):
    collection: str
    documents: list[dict[str, Any]] = Field(min_length=1)


class DeleteDocumentsArgs(BaseModel):
    collection: str
    ids: list[str] = Field(min_length=1)


class SimilaritySearchArgs(BaseModel):
    collection: str
    query: str
    top_k: int = 5
    where: dict[str, Any] | None = None


def chroma_health_check() -> dict:
    return get_chroma_provider().health_check().model_dump()


def chroma_create_collection(collection_name: str) -> dict:
    return get_chroma_provider().create_collection(collection_name).model_dump()


def chroma_delete_collection(collection_name: str) -> dict:
    return get_chroma_provider().delete_collection(collection_name).model_dump()


def chroma_list_collections() -> dict:
    return get_chroma_provider().list_collections().model_dump()


def chroma_add_documents(collection: str, documents: list[dict[str, Any]]) -> dict:
    return get_chroma_provider().add_documents(collection, documents).model_dump()


def chroma_update_documents(collection: str, documents: list[dict[str, Any]]) -> dict:
    return get_chroma_provider().update_documents(collection, documents).model_dump()


def chroma_delete_documents(collection: str, ids: list[str]) -> dict:
    return get_chroma_provider().delete_documents(collection, ids).model_dump()


def chroma_similarity_search(collection: str, query: str, top_k: int = 5, where: dict[str, Any] | None = None) -> dict:
    return get_chroma_provider().similarity_search(collection, query, top_k, where).model_dump()


_TOOLS: list[tuple[str, str, type[BaseModel], object]] = [
    ("chroma_health_check", "Check whether the Chroma vector store backend is reachable", HealthCheckArgs, chroma_health_check),
    ("chroma_create_collection", "Create a collection if it doesn't already exist", CreateCollectionArgs, chroma_create_collection),
    ("chroma_delete_collection", "Delete an entire collection", DeleteCollectionArgs, chroma_delete_collection),
    ("chroma_list_collections", "List all collections", ListCollectionsArgs, chroma_list_collections),
    ("chroma_add_documents", "Add documents (embedding them) to a collection", AddDocumentsArgs, chroma_add_documents),
    ("chroma_update_documents", "Update existing documents in a collection", UpdateDocumentsArgs, chroma_update_documents),
    ("chroma_delete_documents", "Delete documents by id from a collection", DeleteDocumentsArgs, chroma_delete_documents),
    ("chroma_similarity_search", "Semantic similarity search, optionally filtered by metadata", SimilaritySearchArgs, chroma_similarity_search),
]

for _name, _description, _schema, _func in _TOOLS:
    get_tool_registry().register(
        ToolSpec(
            name=_name,
            description=_description,
            input_schema=_schema,
            permissions=["internet_access"],
            retry_policy=_RETRY_POLICY,
            timeout_seconds=_TIMEOUT_SECONDS,
            cost_per_call_usd=0.0,
        ),
        _func,
    )
