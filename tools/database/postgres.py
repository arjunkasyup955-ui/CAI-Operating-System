from typing import Any

from pydantic import BaseModel, Field

from core.registries import ToolSpec, get_tool_registry
from core.registries.tool_registry import RetryPolicy
from tools.database.postgres_providers import get_postgres_provider

_RETRY_POLICY = RetryPolicy(max_attempts=2, backoff_seconds=1.0)
_TIMEOUT_SECONDS = 15.0


class HealthCheckArgs(BaseModel):
    pass


class CreateDatabaseArgs(BaseModel):
    name: str


class CreateTableArgs(BaseModel):
    table_name: str
    columns: dict[str, str]


class SelectArgs(BaseModel):
    table_name: str
    where: dict[str, Any] | None = None


class InsertArgs(BaseModel):
    table_name: str
    values: dict[str, Any] = Field(min_length=1)


class UpdateArgs(BaseModel):
    table_name: str
    where: dict[str, Any] = Field(min_length=1)
    values: dict[str, Any] = Field(min_length=1)


class DeleteArgs(BaseModel):
    table_name: str
    where: dict[str, Any] = Field(min_length=1)


class BeginTransactionArgs(BaseModel):
    pass


class CommitTransactionArgs(BaseModel):
    tx_id: str


class RollbackTransactionArgs(BaseModel):
    tx_id: str


def postgres_health_check() -> dict:
    return get_postgres_provider().health_check().model_dump()


def postgres_create_database(name: str) -> dict:
    return get_postgres_provider().create_database(name).model_dump()


def postgres_create_table(table_name: str, columns: dict[str, str]) -> dict:
    return get_postgres_provider().create_table(table_name, columns).model_dump()


def postgres_select(table_name: str, where: dict[str, Any] | None = None) -> dict:
    return get_postgres_provider().execute_select(table_name, where).model_dump()


def postgres_insert(table_name: str, values: dict[str, Any]) -> dict:
    return get_postgres_provider().execute_insert(table_name, values).model_dump()


def postgres_update(table_name: str, where: dict[str, Any], values: dict[str, Any]) -> dict:
    return get_postgres_provider().execute_update(table_name, where, values).model_dump()


def postgres_delete(table_name: str, where: dict[str, Any]) -> dict:
    return get_postgres_provider().execute_delete(table_name, where).model_dump()


def postgres_begin_transaction() -> dict:
    tx_id = get_postgres_provider().begin_transaction()
    return {"tx_id": tx_id}


def postgres_commit_transaction(tx_id: str) -> dict:
    get_postgres_provider().commit_transaction(tx_id)
    return {"tx_id": tx_id, "committed": True}


def postgres_rollback_transaction(tx_id: str) -> dict:
    get_postgres_provider().rollback_transaction(tx_id)
    return {"tx_id": tx_id, "rolled_back": True}


_TOOLS: list[tuple[str, str, type[BaseModel], object]] = [
    ("postgres_health_check", "Check whether the configured PostgreSQL server is reachable", HealthCheckArgs, postgres_health_check),
    ("postgres_create_database", "Create a new PostgreSQL database", CreateDatabaseArgs, postgres_create_database),
    ("postgres_create_table", "Create a table if it doesn't already exist", CreateTableArgs, postgres_create_table),
    ("postgres_select", "Select rows from a table, optionally filtered", SelectArgs, postgres_select),
    ("postgres_insert", "Insert a row into a table", InsertArgs, postgres_insert),
    ("postgres_update", "Update rows matching a filter", UpdateArgs, postgres_update),
    ("postgres_delete", "Delete rows matching a filter", DeleteArgs, postgres_delete),
    ("postgres_begin_transaction", "Begin a transaction, returning a transaction id", BeginTransactionArgs, postgres_begin_transaction),
    ("postgres_commit_transaction", "Commit a transaction by id", CommitTransactionArgs, postgres_commit_transaction),
    ("postgres_rollback_transaction", "Roll back a transaction by id", RollbackTransactionArgs, postgres_rollback_transaction),
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
