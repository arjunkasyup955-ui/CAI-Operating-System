import os
from typing import Any, Protocol

from dotenv import load_dotenv
from pydantic import BaseModel

# Deliberately self-contained (own env reading, not core.config.Settings) so this tool
# module never needs the frozen Phase 0 kernel to change to gain a new config key -
# same convention as tools/web, tools/browser, tools/ollama.
load_dotenv()


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


class PostgresConnectionConfig(BaseModel):
    host: str = "localhost"
    port: int = 5432
    database: str = "postgres"
    user: str = "postgres"
    password: str = ""
    min_pool_size: int = 1
    max_pool_size: int = 5


class PostgresHealthStatus(BaseModel):
    healthy: bool
    host: str
    port: int
    version: str = ""
    error: str = ""


class PostgresQueryResult(BaseModel):
    success: bool
    operation: str
    rows: list[dict[str, Any]] = []
    row_count: int = 0
    error: str = ""


class PostgresProvider(Protocol):
    """Connection management + pooling + CRUD + transactions. Every method either
    returns a *Result on success or raises on failure (connection-level or
    query-level) - the agent layer's try/except (with ToolRegistry's RetryPolicy)
    handles graceful degradation uniformly, the same pattern as every prior provider.

    Structured (table/where/values), not raw SQL - keeps the fake and real providers
    genuinely interchangeable, and values are always sent as parameters (never string-
    interpolated), which is what actually prevents SQL injection here. Table/column
    identifiers are interpolated (standard practice - identifiers can't be
    parameterized in SQL) and are trusted to come from agent code, not raw user input.
    """

    name: str

    def health_check(self) -> PostgresHealthStatus: ...
    def create_database(self, name: str) -> PostgresQueryResult: ...
    def create_table(self, table_name: str, columns: dict[str, str]) -> PostgresQueryResult: ...
    def execute_select(self, table_name: str, where: dict[str, Any] | None = None) -> PostgresQueryResult: ...
    def execute_insert(self, table_name: str, values: dict[str, Any]) -> PostgresQueryResult: ...
    def execute_update(self, table_name: str, where: dict[str, Any], values: dict[str, Any]) -> PostgresQueryResult: ...
    def execute_delete(self, table_name: str, where: dict[str, Any]) -> PostgresQueryResult: ...
    def begin_transaction(self) -> str: ...
    def commit_transaction(self, tx_id: str) -> None: ...
    def rollback_transaction(self, tx_id: str) -> None: ...


class PsycopgPostgresProvider:
    """Default PostgresProvider - real connection pooling and SQL execution via
    psycopg. Lazily imported so this module loads fine even when psycopg isn't
    installed (it isn't, in this sandbox - no Postgres server is running here either);
    a call then raises a clear, actionable error, exactly the same pattern as
    PlaywrightProvider/Crawl4AIProvider in tools/browser/browser_providers.py.
    """

    name = "psycopg"

    def __init__(self, config: PostgresConnectionConfig | None = None) -> None:
        self._config = config or PostgresConnectionConfig(
            host=_env("POSTGRES_HOST", "localhost"),
            port=int(_env("POSTGRES_PORT", "5432")),
            database=_env("POSTGRES_DATABASE", "postgres"),
            user=_env("POSTGRES_USER", "postgres"),
            password=_env("POSTGRES_PASSWORD", ""),
        )
        self._pool = None

    def _get_pool(self):
        if self._pool is None:
            try:
                from psycopg_pool import ConnectionPool
            except ImportError as exc:
                raise RuntimeError(
                    "psycopg[binary] and psycopg_pool are not installed - "
                    '`pip install "psycopg[binary,pool]"`'
                ) from exc
            conninfo = (
                f"host={self._config.host} port={self._config.port} "
                f"dbname={self._config.database} user={self._config.user} password={self._config.password}"
            )
            self._pool = ConnectionPool(
                conninfo, min_size=self._config.min_pool_size, max_size=self._config.max_pool_size, open=True
            )
        return self._pool

    def health_check(self) -> PostgresHealthStatus:
        try:
            pool = self._get_pool()
            with pool.connection(timeout=5) as conn, conn.cursor() as cur:
                cur.execute("SELECT version()")
                version = cur.fetchone()[0]
            return PostgresHealthStatus(healthy=True, host=self._config.host, port=self._config.port, version=version)
        except Exception as exc:
            return PostgresHealthStatus(healthy=False, host=self._config.host, port=self._config.port, error=str(exc))

    def create_database(self, name: str) -> PostgresQueryResult:
        pool = self._get_pool()
        with pool.connection(timeout=10) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(f'CREATE DATABASE "{name}"')
        return PostgresQueryResult(success=True, operation="create_database", row_count=0)

    def create_table(self, table_name: str, columns: dict[str, str]) -> PostgresQueryResult:
        cols_sql = ", ".join(f'"{col}" {coltype}' for col, coltype in columns.items())
        ddl = f'CREATE TABLE IF NOT EXISTS "{table_name}" ({cols_sql})'
        pool = self._get_pool()
        with pool.connection(timeout=10) as conn, conn.cursor() as cur:
            cur.execute(ddl)
        return PostgresQueryResult(success=True, operation="create_table", row_count=0)

    def execute_select(self, table_name: str, where: dict[str, Any] | None = None) -> PostgresQueryResult:
        from psycopg.rows import dict_row

        query = f'SELECT * FROM "{table_name}"'
        params: list[Any] = []
        if where:
            query += " WHERE " + " AND ".join(f'"{k}" = %s' for k in where)
            params = list(where.values())

        pool = self._get_pool()
        with pool.connection(timeout=10) as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, params)
            rows = [dict(r) for r in cur.fetchall()]
        return PostgresQueryResult(success=True, operation="select", rows=rows, row_count=len(rows))

    def execute_insert(self, table_name: str, values: dict[str, Any]) -> PostgresQueryResult:
        cols = ", ".join(f'"{k}"' for k in values)
        placeholders = ", ".join(["%s"] * len(values))
        query = f'INSERT INTO "{table_name}" ({cols}) VALUES ({placeholders})'
        pool = self._get_pool()
        with pool.connection(timeout=10) as conn, conn.cursor() as cur:
            cur.execute(query, list(values.values()))
            row_count = cur.rowcount
            conn.commit()
        return PostgresQueryResult(success=True, operation="insert", row_count=row_count)

    def execute_update(self, table_name: str, where: dict[str, Any], values: dict[str, Any]) -> PostgresQueryResult:
        set_clause = ", ".join(f'"{k}" = %s' for k in values)
        where_clause = " AND ".join(f'"{k}" = %s' for k in where)
        query = f'UPDATE "{table_name}" SET {set_clause} WHERE {where_clause}'
        params = [*values.values(), *where.values()]
        pool = self._get_pool()
        with pool.connection(timeout=10) as conn, conn.cursor() as cur:
            cur.execute(query, params)
            row_count = cur.rowcount
            conn.commit()
        return PostgresQueryResult(success=True, operation="update", row_count=row_count)

    def execute_delete(self, table_name: str, where: dict[str, Any]) -> PostgresQueryResult:
        where_clause = " AND ".join(f'"{k}" = %s' for k in where)
        query = f'DELETE FROM "{table_name}" WHERE {where_clause}'
        pool = self._get_pool()
        with pool.connection(timeout=10) as conn, conn.cursor() as cur:
            cur.execute(query, list(where.values()))
            row_count = cur.rowcount
            conn.commit()
        return PostgresQueryResult(success=True, operation="delete", row_count=row_count)

    def begin_transaction(self) -> str:
        raise RuntimeError(
            "explicit cross-call transactions require a held connection - not yet wired up for the "
            "real pooled provider; use the fake provider (deterministic, in-memory) to exercise "
            "transaction/rollback semantics, or extend this provider to check out and hold a single "
            "pooled connection per transaction id."
        )

    def commit_transaction(self, tx_id: str) -> None:
        raise RuntimeError("see begin_transaction - not yet wired up for the real pooled provider")

    def rollback_transaction(self, tx_id: str) -> None:
        raise RuntimeError("see begin_transaction - not yet wired up for the real pooled provider")


class FakePostgresProvider:
    """In-memory PostgresProvider - deterministic, no real network/server needed.
    Genuinely supports transactions via snapshot-and-restore, so rollback can be
    tested for real rather than just asserted as a no-op.
    """

    name = "fake_postgres"

    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {}
        self._tx_snapshots: dict[str, dict[str, list[dict[str, Any]]]] = {}
        self._tx_counter = 0

    def health_check(self) -> PostgresHealthStatus:
        return PostgresHealthStatus(healthy=True, host="fake", port=0, version="fake-postgres-1.0")

    def create_database(self, name: str) -> PostgresQueryResult:
        return PostgresQueryResult(success=True, operation="create_database", row_count=0)

    def create_table(self, table_name: str, columns: dict[str, str]) -> PostgresQueryResult:
        self._tables.setdefault(table_name, [])
        return PostgresQueryResult(success=True, operation="create_table", row_count=0)

    def execute_select(self, table_name: str, where: dict[str, Any] | None = None) -> PostgresQueryResult:
        rows = self._tables.get(table_name, [])
        if where:
            rows = [r for r in rows if all(r.get(k) == v for k, v in where.items())]
        return PostgresQueryResult(success=True, operation="select", rows=[dict(r) for r in rows], row_count=len(rows))

    def execute_insert(self, table_name: str, values: dict[str, Any]) -> PostgresQueryResult:
        self._tables.setdefault(table_name, []).append(dict(values))
        return PostgresQueryResult(success=True, operation="insert", row_count=1)

    def execute_update(self, table_name: str, where: dict[str, Any], values: dict[str, Any]) -> PostgresQueryResult:
        rows = self._tables.get(table_name, [])
        count = 0
        for row in rows:
            if all(row.get(k) == v for k, v in where.items()):
                row.update(values)
                count += 1
        return PostgresQueryResult(success=True, operation="update", row_count=count)

    def execute_delete(self, table_name: str, where: dict[str, Any]) -> PostgresQueryResult:
        rows = self._tables.get(table_name, [])
        remaining = [r for r in rows if not all(r.get(k) == v for k, v in where.items())]
        deleted = len(rows) - len(remaining)
        self._tables[table_name] = remaining
        return PostgresQueryResult(success=True, operation="delete", row_count=deleted)

    def begin_transaction(self) -> str:
        self._tx_counter += 1
        tx_id = f"tx-{self._tx_counter}"
        self._tx_snapshots[tx_id] = {table: [dict(r) for r in rows] for table, rows in self._tables.items()}
        return tx_id

    def commit_transaction(self, tx_id: str) -> None:
        if tx_id not in self._tx_snapshots:
            raise ValueError(f"unknown transaction id: {tx_id}")
        self._tx_snapshots.pop(tx_id, None)

    def rollback_transaction(self, tx_id: str) -> None:
        snapshot = self._tx_snapshots.pop(tx_id, None)
        if snapshot is None:
            raise ValueError(f"unknown transaction id: {tx_id}")
        self._tables = snapshot


_provider: PostgresProvider = PsycopgPostgresProvider()


def get_postgres_provider() -> PostgresProvider:
    return _provider


def set_postgres_provider(provider: PostgresProvider) -> None:
    """Swappable, not cached - lets tests inject a deterministic fake provider."""
    global _provider
    _provider = provider
