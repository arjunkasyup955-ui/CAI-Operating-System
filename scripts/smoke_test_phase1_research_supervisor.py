"""Phase 1, Component 1: Research Supervisor subgraph skeleton.

Verifies:
  1. The happy path - manager -> approval_gate (approved) -> research_supervisor -
     runs end to end with no double-accumulation of reducer fields (history,
     research_findings) across the subgraph boundary.
  2. The rejection path - a rejected approval must NOT run the Research Supervisor
     at all (a latent Phase 0 gap that becomes a real bug once something follows
     the approval gate - see _route_after_approval in workflows/main_graph.py).

Run: python scripts/smoke_test_phase1_research_supervisor.py
"""

import logging
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from langgraph.types import Command  # noqa: E402

from core.memory_gateway import get_memory_gateway  # noqa: E402
from workflows.main_graph import compile_main_graph  # noqa: E402


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"test failed at: {label}")


def main() -> None:
    gateway = get_memory_gateway()

    print("\n== Happy path: approved -> Research Supervisor runs ==")
    with gateway.working() as checkpointer:
        graph = compile_main_graph(checkpointer)
        # A fresh thread_id every run - the checkpointer is persistent SQLite, so a
        # fixed id would accumulate state across separate script executions and make
        # these assertions non-deterministic depending on how many times this ran before.
        venture_id = f"venture-research-approved-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": venture_id}}
        initial_state = {
            "venture_id": venture_id,
            "idea": "A marketplace for solo founders to hire vetted freelance CFOs",
            "phase": "idea",
        }

        first = graph.invoke(initial_state, config=config)
        check("paused on approval interrupt", "__interrupt__" in first)

        final = graph.invoke(Command(resume={"approved": True, "reason": "approved for research"}), config=config)
        check("graph completed (no further interrupt)", "__interrupt__" not in final)

        # Checked per-agent (not as an absolute total) so this stays valid as more
        # workers (Browser, Market Intelligence, ...) are added inside the Supervisor.
        findings = final.get("research_findings", [])
        intake_findings = [f for f in findings if f.get("agent") == "research_supervisor"]
        check("research_supervisor produced exactly one finding (no duplication)", len(intake_findings) == 1)

        intake_history = [h for h in final["history"] if h.get("agent") == "research_supervisor"]
        check("exactly one research_supervisor history entry (no duplication)", len(intake_history) == 1)

        manager_history = [h for h in final["history"] if h.get("event") == "received_idea"]
        check("manager's own history entry appears exactly once too", len(manager_history) == 1)

    print("\n== Rejection path: rejected -> Research Supervisor must NOT run ==")
    with gateway.working() as checkpointer:
        graph = compile_main_graph(checkpointer)
        venture_id = f"venture-research-rejected-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": venture_id}}
        initial_state = {
            "venture_id": venture_id,
            "idea": "A crypto casino for teenagers",
            "phase": "idea",
        }

        first = graph.invoke(initial_state, config=config)
        check("paused on approval interrupt", "__interrupt__" in first)

        final = graph.invoke(Command(resume={"approved": False, "reason": "not a venture we pursue"}), config=config)
        check("graph completed (no further interrupt)", "__interrupt__" not in final)
        check("research_findings is empty - research never ran", not final.get("research_findings"))
        check(
            "no research_supervisor history entry - research never ran",
            not [h for h in final["history"] if h.get("agent") == "research_supervisor"],
        )

    print("\nAll Phase 1 Component 1 checks passed.")


if __name__ == "__main__":
    main()
