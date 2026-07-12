"""Install verification for AFOS.

Run this immediately after `pip install -r requirements.txt` (inside the
project's virtual environment) to confirm the installed dependency closure
is actually usable - in particular, that every LangGraph import pattern used
anywhere in this codebase resolves, including the split-package checkpoint
backends (langgraph-checkpoint-sqlite provides `langgraph.checkpoint.sqlite`;
it is a separate package from langgraph-checkpoint and is easy to
accidentally omit without a pinned requirements.txt).

Usage:
    .venv\\Scripts\\activate
    pip install -r requirements.txt
    python scripts/verify_install.py
"""

import sys


def check(label: str, fn) -> bool:
    try:
        fn()
        print(f"[OK] {label}")
        return True
    except Exception as exc:  # noqa: BLE001 - we want to report ANY import failure, not just ImportError
        print(f"[FAIL] {label}: {exc}")
        return False


def main() -> None:
    results = []

    results.append(check("langgraph.errors.GraphBubbleUp / GraphInterrupt", lambda: __import__("langgraph.errors", fromlist=["GraphBubbleUp", "GraphInterrupt"])))
    results.append(check("langgraph.graph.{END, START, StateGraph}", lambda: __import__("langgraph.graph", fromlist=["END", "START", "StateGraph"])))
    results.append(check("langgraph.graph.state.CompiledStateGraph", lambda: __import__("langgraph.graph.state", fromlist=["CompiledStateGraph"])))
    results.append(check("langgraph.checkpoint.memory.InMemorySaver", lambda: __import__("langgraph.checkpoint.memory", fromlist=["InMemorySaver"])))
    results.append(check("langgraph.checkpoint.base.BaseCheckpointSaver", lambda: __import__("langgraph.checkpoint.base", fromlist=["BaseCheckpointSaver"])))
    results.append(check("langgraph.checkpoint.sqlite.SqliteSaver (the package the user's report flagged)", lambda: __import__("langgraph.checkpoint.sqlite", fromlist=["SqliteSaver"])))
    results.append(check("langgraph.types.{Command, RetryPolicy, interrupt}", lambda: __import__("langgraph.types", fromlist=["Command", "RetryPolicy", "interrupt"])))

    def _instantiate_sqlite_saver():
        from langgraph.checkpoint.sqlite import SqliteSaver
        import sqlite3

        conn = sqlite3.connect(":memory:")
        SqliteSaver(conn)

    results.append(check("SqliteSaver actually constructs against a real sqlite3 connection", _instantiate_sqlite_saver))

    def _build_and_compile_a_graph():
        from langgraph.graph import END, START, StateGraph
        from typing import TypedDict

        class _State(TypedDict):
            x: int

        graph = StateGraph(_State)
        graph.add_node("noop", lambda state: {})
        graph.add_edge(START, "noop")
        graph.add_edge("noop", END)
        compiled = graph.compile()
        compiled.invoke({"x": 1})

    results.append(check("a real StateGraph builds, compiles, and invokes end-to-end", _build_and_compile_a_graph))

    print()
    if all(results):
        print(f"PASS - {len(results)}/{len(results)} checks succeeded")
        sys.exit(0)
    else:
        failed = sum(1 for r in results if not r)
        print(f"FAIL - {failed}/{len(results)} checks failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
