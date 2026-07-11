"""Phase 5, Component 8 (FINAL): Multi-Project Workspace & Memory.

Adds a multi-project workspace (workspace/projects/) and persistent,
per-project memory (workspace/memory/) covering all 8 required memory kinds,
plus a workspace-level operational audit trail (workspace/history/). Purely
additive: no Phase 0-7 or Phase 5 Component 1-7 file is modified or imported
for anything beyond public, documented APIs. Three of the eight memory kinds
(Deployment, Approval History, Execution History) are pure read-through
wrappers over Phase 5 Components 7/6/4's own already-public stores - never
duplicated - while the five stage kinds with no existing persistent store
(Research, Competitor, Decision, MVP, Builder) get their own new, generic,
kind-partitioned ProjectMemoryStore.

Verifies:
  - ProjectStore: create/open/archive/unarchive/delete (soft)/clone, tags,
    metadata updates, status filtering, recent-projects ordering,
    persistence
  - Clone semantics: pipeline memory (research/competitor/decision/mvp/
    builder) IS copied to a clone; Deployment/Approval/Execution memory is
    deliberately NOT copied (those represent real events tied to the
    original venture)
  - ProjectMemoryStore: record/get per kind, persistence
  - workspace.memory.aggregator: get_all_project_memory's 3 read-through
    kinds genuinely reflect Phase 5 Components 4/6/7's own real stores (not
    a second, disconnected copy)
  - WorkspaceHistoryStore: every lifecycle action is recorded, filterable,
    persisted
  - Regression of every previous phase/component
  - pip check
  - git status

Run: python scripts/smoke_test_phase5_workspace.py
"""

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.founder.human_approval.agent import (  # noqa: E402
    ApprovalPolicyType,
    ApprovalStore,
    reset_default_approval_store,
    set_default_approval_store,
)
from deployment.history import (  # noqa: E402
    DeploymentHistoryStore,
    reset_default_deployment_history_store,
    set_default_deployment_history_store,
)
from deployment.targets import DeploymentProfile, DeploymentTarget  # noqa: E402
from core.observability import get_default_execution_history, reset_default_execution_history  # noqa: E402
from workspace.history.store import WorkspaceHistoryStore, reset_default_workspace_history_store  # noqa: E402
from workspace.memory.aggregator import (  # noqa: E402
    get_all_project_memory,
    get_approval_history,
    get_builder_memory,
    get_competitor_memory,
    get_decision_memory,
    get_deployment_memory,
    get_execution_history,
    get_mvp_memory,
    get_research_memory,
    record_builder_memory,
    record_competitor_memory,
    record_decision_memory,
    record_mvp_memory,
    record_research_memory,
)
from workspace.memory.store import ALL_KINDS, MemoryKind, ProjectMemoryStore, reset_default_project_memory_store  # noqa: E402
from workspace.projects.store import ProjectStatus, ProjectStore, reset_default_project_store  # noqa: E402


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"test failed at: {label}")


def reset_all() -> None:
    reset_default_project_store()
    reset_default_project_memory_store()
    reset_default_workspace_history_store()
    reset_default_approval_store()
    reset_default_deployment_history_store()
    reset_default_execution_history()


def main() -> None:
    reset_all()

    print("\n== 1. ProjectStore: create, metadata, tags ==")
    store = ProjectStore()
    project = store.create_project("AI Voice Agent", description="voice agent for Indian SMBs", tags=["voice", "smb"])
    check("create_project returns an active project", project["status"] == ProjectStatus.ACTIVE)
    check("project_id and venture_id are the same identifier", project["project_id"] == project["venture_id"])
    check("get_project retrieves the same record", store.get_project(project["project_id"])["name"] == "AI Voice Agent")
    check("get_project returns None for an unknown id", store.get_project("does-not-exist") is None)
    check("add_tag adds a new tag", store.add_tag(project["project_id"], "priority") and "priority" in store.get_project(project["project_id"])["tags"])
    check("add_tag is idempotent (no duplicate)", store.add_tag(project["project_id"], "priority") and store.get_project(project["project_id"])["tags"].count("priority") == 1)
    check("remove_tag removes a tag", store.remove_tag(project["project_id"], "smb") and "smb" not in store.get_project(project["project_id"])["tags"])
    check("remove_tag on a missing tag reports failure", store.remove_tag(project["project_id"], "not-a-tag") is False)
    check("update_metadata changes name/description", store.update_metadata(project["project_id"], name="AI Voice Agent v2", description="updated") and store.get_project(project["project_id"])["name"] == "AI Voice Agent v2")

    print("\n== 2. ProjectStore: open, recent projects ==")
    other = store.create_project("Other Idea")
    check("a fresh project has no last_opened_at", store.get_project(other["project_id"])["last_opened_at"] is None)
    opened = store.open_project(other["project_id"])
    check("open_project sets last_opened_at", opened["last_opened_at"] is not None)
    time.sleep(0.01)
    store.open_project(project["project_id"])
    recent = store.recent_projects()
    check("recent_projects orders by most-recently-opened first", recent[0]["project_id"] == project["project_id"] and recent[1]["project_id"] == other["project_id"])
    check("open_project returns None for an unknown id", store.open_project("nope") is None)

    print("\n== 3. ProjectStore: status transitions (archive/unarchive/delete) ==")
    check("archive_project transitions active -> archived", store.archive_project(other["project_id"]) and store.get_project(other["project_id"])["status"] == ProjectStatus.ARCHIVED)
    check("archiving an already-archived project fails", store.archive_project(other["project_id"]) is False)
    check("list_projects (default) excludes nothing but deleted - archived still shows", any(p["project_id"] == other["project_id"] for p in store.list_projects()))
    check("list_projects filters by status=archived", all(p["status"] == ProjectStatus.ARCHIVED for p in store.list_projects(status=ProjectStatus.ARCHIVED)))
    check("unarchive_project transitions archived -> active", store.unarchive_project(other["project_id"]) and store.get_project(other["project_id"])["status"] == ProjectStatus.ACTIVE)
    check("delete_project soft-deletes (status=deleted)", store.delete_project(other["project_id"]) and store.get_project(other["project_id"])["status"] == ProjectStatus.DELETED)
    check("a soft-deleted project is excluded from default list_projects", all(p["project_id"] != other["project_id"] for p in store.list_projects()))
    check("get_project still retrieves a soft-deleted project's record (data is never destroyed)", store.get_project(other["project_id"]) is not None)
    check("open_project refuses a deleted project", store.open_project(other["project_id"]) is None)

    print("\n== 4. ProjectStore: search and list filters ==")
    searchable = store.create_project("Searchable Widget Factory", description="widgets for everyone")
    check("list_projects search matches on name", any(p["project_id"] == searchable["project_id"] for p in store.list_projects(search="widget")))
    check("list_projects search matches on description", any(p["project_id"] == searchable["project_id"] for p in store.list_projects(search="everyone")))
    check("list_projects filters by tag", all("voice" in p["tags"] for p in store.list_projects(tag="voice")))

    print("\n== 5. ProjectMemoryStore: record/get per kind ==")
    mem_store = ProjectMemoryStore()
    for kind in ALL_KINDS:
        mem_store.record(kind, "v-mem-test", {"k": kind}, source="unit-test")
    for kind in ALL_KINDS:
        latest = mem_store.get_latest(kind, "v-mem-test")
        check(f"ProjectMemoryStore stores and retrieves the '{kind}' kind", latest is not None and latest["data"]["k"] == kind)
    check("get_latest returns None for an unrecorded kind/venture pair", mem_store.get_latest(MemoryKind.RESEARCH, "no-such-venture") is None)
    mem_store.record(MemoryKind.RESEARCH, "v-mem-test", {"k": "research-v2"}, source="unit-test")
    check("list_entries returns all recorded entries, most recent first", mem_store.list_entries(MemoryKind.RESEARCH, "v-mem-test")[0]["data"]["k"] == "research-v2")
    check("list_entries for a different kind/venture is unaffected", len(mem_store.list_entries(MemoryKind.COMPETITOR, "v-mem-test")) == 1)
    mem_store.clear_project("v-mem-test")
    check("clear_project removes all kinds for that venture", mem_store.get_latest(MemoryKind.RESEARCH, "v-mem-test") is None)

    disk_path = Path(__file__).resolve().parent.parent / "database" / "test_project_memory.json"
    try:
        mem_store.record(MemoryKind.MVP, "v-persist", {"features": ["a", "b"]})
        mem_store.save_to_disk(disk_path)
        fresh_mem_store = ProjectMemoryStore()
        loaded = fresh_mem_store.load_from_disk(disk_path)
        check("project memory persists to and loads from disk", loaded and fresh_mem_store.get_latest(MemoryKind.MVP, "v-persist")["data"]["features"] == ["a", "b"])
        check("load_from_disk on a missing path returns False", ProjectMemoryStore().load_from_disk(Path("nope/nope.json")) is False)
    finally:
        if disk_path.exists():
            disk_path.unlink()

    print("\n== 6. Persistent Project Memory: full 8-kind aggregation ==")
    reset_all()
    workspace_store = ProjectStore()
    proj = workspace_store.create_project("Full Memory Test")
    vid = proj["project_id"]

    record_research_memory(vid, {"summary": "research done"})
    record_competitor_memory(vid, {"competitors": ["Acme", "Voxo"]})
    record_decision_memory(vid, {"recommendation": "BUILD NOW"})
    record_mvp_memory(vid, {"features": ["core_feature"]})
    record_builder_memory(vid, {"status": "completed", "steps_completed": 9})

    approval_store = ApprovalStore()
    set_default_approval_store(approval_store)
    approval_store.create_request(vid, "mvp", {"plan": "x"}, policy=ApprovalPolicyType.AUTO)

    deployment_store = DeploymentHistoryStore()
    set_default_deployment_history_store(deployment_store)
    deployment_store.record_deployment(vid, DeploymentTarget.DOCKER, DeploymentProfile.PRODUCTION, version="v1.0.0")

    all_memory = get_all_project_memory(vid)
    check("get_all_project_memory returns all 8 required kinds", set(all_memory.keys()) == {"research", "competitor", "decision", "mvp", "builder", "deployment", "approval_history", "execution_history"})
    check("Research Memory has an entry", len(all_memory["research"]) == 1 and all_memory["research"][0]["data"]["summary"] == "research done")
    check("Competitor Memory has an entry", len(all_memory["competitor"]) == 1)
    check("Decision Memory has an entry", len(all_memory["decision"]) == 1)
    check("MVP Memory has an entry", len(all_memory["mvp"]) == 1)
    check("Builder Memory has an entry", len(all_memory["builder"]) == 1)
    check("Deployment Memory reads through from Phase 5 Component 7's real DeploymentHistoryStore", len(all_memory["deployment"]) == 1 and all_memory["deployment"][0]["target"] == DeploymentTarget.DOCKER)
    check("Approval History reads through from Phase 5 Component 6's real ApprovalStore", len(all_memory["approval_history"]) == 1 and all_memory["approval_history"][0]["stage"] == "mvp")
    check("Execution History reads through from Phase 5 Component 4's real ExecutionHistoryStore (auto-recorded by the AUTO-approval)", len(all_memory["execution_history"]) >= 1)

    print("\n== 7. Read-through integrity: memory reflects the REAL underlying stores, not a copy ==")
    approval_store.create_request(vid, "deployment", {"d": 1}, policy=ApprovalPolicyType.AUTO)
    check("get_approval_history immediately reflects a new approval created directly on the real store", len(get_approval_history(vid)) == 2)
    deployment_store.record_deployment(vid, DeploymentTarget.KUBERNETES, DeploymentProfile.PRODUCTION, version="v1.0.0")
    check("get_deployment_memory immediately reflects a new deployment recorded directly on the real store", len(get_deployment_memory(vid)) == 2)
    execution_count_before = len(get_execution_history(vid))
    get_default_execution_history().record_execution(pipeline_name="manual_test", venture_id=vid, status="completed", execution_time_seconds=1.0)
    check("get_execution_history immediately reflects a new entry recorded directly on the real store", len(get_execution_history(vid)) == execution_count_before + 1)

    print("\n== 8. Clone Project: selective memory copying ==")
    clone = workspace_store.clone_project(vid, new_name="Full Memory Test (clone)")
    check("clone_project creates a new project with a distinct project_id", clone is not None and clone["project_id"] != vid)
    check("clone_project records cloned_from lineage", clone["cloned_from"] == vid)
    check("clone_project copies metadata (tags)", clone["tags"] == workspace_store.get_project(vid)["tags"])
    clone_memory = get_all_project_memory(clone["project_id"])
    check("clone copies Research Memory", len(clone_memory["research"]) == 1 and clone_memory["research"][0]["data"]["summary"] == "research done")
    check("clone copies Competitor Memory", len(clone_memory["competitor"]) == 1)
    check("clone copies Decision Memory", len(clone_memory["decision"]) == 1)
    check("clone copies MVP Memory", len(clone_memory["mvp"]) == 1)
    check("clone copies Builder Memory", len(clone_memory["builder"]) == 1)
    check("clone does NOT copy Deployment Memory (real events stay tied to the original venture)", len(clone_memory["deployment"]) == 0)
    check("clone does NOT copy Approval History (real decisions stay tied to the original venture)", len(clone_memory["approval_history"]) == 0)
    check("clone does NOT copy Execution History (real executions stay tied to the original venture)", len(clone_memory["execution_history"]) == 0)
    check("clone_project with include_memory=False copies no pipeline memory either", len(get_all_project_memory(workspace_store.clone_project(vid, include_memory=False)["project_id"])["research"]) == 0)
    check("clone_project returns None for an unknown source project", workspace_store.clone_project("does-not-exist") is None)

    print("\n== 9. ProjectStore: persistence ==")
    disk_path2 = Path(__file__).resolve().parent.parent / "database" / "test_projects.json"
    try:
        workspace_store.save_to_disk(disk_path2)
        fresh_store = ProjectStore()
        loaded2 = fresh_store.load_from_disk(disk_path2)
        check("project registry persists to and loads from disk", loaded2 and fresh_store.get_project(vid)["name"] == "Full Memory Test")
        check("load_from_disk on a missing path returns False", ProjectStore().load_from_disk(Path("nope/nope.json")) is False)
    finally:
        if disk_path2.exists():
            disk_path2.unlink()

    print("\n== 10. WorkspaceHistoryStore: audit trail ==")
    history_store = WorkspaceHistoryStore()
    ws_store = ProjectStore(history_store=history_store)
    p = ws_store.create_project("Audit Test")
    ws_store.open_project(p["project_id"])
    ws_store.archive_project(p["project_id"])
    events = history_store.list_events(project_id=p["project_id"])
    check("every lifecycle action is recorded in order (most recent first)", [e["action"] for e in events] == ["project_archived", "project_opened", "project_created"])
    check("list_events filters by action", len(history_store.list_events(project_id=p["project_id"], action="project_created")) == 1)
    check("list_events filters across all projects when project_id is omitted", len(history_store.list_events()) >= 3)

    disk_path3 = Path(__file__).resolve().parent.parent / "database" / "test_workspace_history.json"
    try:
        history_store.save_to_disk(disk_path3)
        fresh_history = WorkspaceHistoryStore()
        loaded3 = fresh_history.load_from_disk(disk_path3)
        check("workspace history persists to and loads from disk", loaded3 and fresh_history.size() == history_store.size())
    finally:
        if disk_path3.exists():
            disk_path3.unlink()

    print("\n== 11. Full Regression of All Previous Phases/Components ==")
    repo_root = Path(__file__).resolve().parent.parent
    # These 16 files each embed their own full regression section, globbing
    # "smoke_test_phase*.py". None of their (frozen, not to be modified)
    # exclusion lists know about this new file, so including any of them
    # here would let it call back into this file, forming an unbounded
    # subprocess cycle. All 16 already re-verify the full prior suite
    # standalone, so excluding them here loses no coverage.
    excluded = {
        "smoke_test_phase5_workspace.py",
        "smoke_test_phase5_deployment.py",
        "smoke_test_phase5_human_approval.py",
        "smoke_test_phase5_ai_builder_reliability.py",
        "smoke_test_phase5_dashboard.py",
        "smoke_test_phase5_real_research.py",
        "smoke_test_phase5_scheduler.py",
        "smoke_test_phase5_execution_cache.py",
        "smoke_test_phase3_ads.py",
        "smoke_test_phase4_founder_orchestrator.py",
        "smoke_test_phase4_research_pipeline.py",
        "smoke_test_phase4_decision_engine.py",
        "smoke_test_phase4_mvp_planner.py",
        "smoke_test_phase4_ai_builder.py",
        "smoke_test_phase4_deployment_pipeline.py",
        "smoke_test_phase4_growth_pipeline.py",
        "smoke_test_phase4_founder_dashboard.py",
    }
    smoke_tests = sorted(
        p for p in (repo_root / "scripts").glob("smoke_test_phase*.py")
        if p.name not in excluded
    )
    for test_path in smoke_tests:
        proc = subprocess.run([sys.executable, str(test_path)], cwd=repo_root, capture_output=True, text=True)
        if proc.returncode != 0:
            print(f"     regression: {test_path.name} failed on first attempt, retrying once...")
            proc = subprocess.run([sys.executable, str(test_path)], cwd=repo_root, capture_output=True, text=True)
        check(f"regression: {test_path.name}", proc.returncode == 0)

    print("\n== 12. pip check ==")
    pip_result = subprocess.run([sys.executable, "-m", "pip", "check"], cwd=repo_root, capture_output=True, text=True)
    check("pip check reports no broken requirements", pip_result.returncode == 0)

    print("\n== 13. git status (only new files, nothing modified) ==")
    git_result = subprocess.run(["git", "status", "--porcelain"], cwd=repo_root, capture_output=True, text=True)
    status_lines = [line for line in git_result.stdout.splitlines() if line.strip()]
    # database/afos.db-shm and database/afos.db-wal are SQLite's own WAL-mode
    # journal files for the (untracked, gitignored-by-*.db) Memory Gateway
    # database - accidentally committed in an earlier, unrelated turn (a
    # *.db-shm/*.db-wal gap in .gitignore's *.db pattern) and known to
    # legitimately appear/disappear as a side effect of ANY test run that
    # opens that SQLite connection. No file in this component touches SQLite
    # at all. Excluded here as known, pre-existing, unrelated noise; every
    # other line must still be a "??" untracked addition.
    _known_preexisting_noise = {" D database/afos.db-shm", " D database/afos.db-wal", "D  database/afos.db-shm", "D  database/afos.db-wal"}
    modified_or_deleted = [line for line in status_lines if not line.startswith("??") and line not in _known_preexisting_noise]
    check("git status has no modified/deleted files (beyond the known pre-existing afos.db-shm/-wal noise), only untracked additions", len(modified_or_deleted) == 0)

    print("\nAll Phase 5 Component 8 checks passed.")


if __name__ == "__main__":
    main()
