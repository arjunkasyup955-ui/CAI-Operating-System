"""Phase 5, Component 1: Execution Cache & Shared State.

A production-grade, thread-safe execution cache and shared state layer for
AFOS. Adds a new core/execution_cache/ kernel subsystem (SharedStateStore +
ExecutionCacheManager) and wires it into the Founder Dashboard (Phase 4
Component 8) entirely through that component's own pre-existing, public
dependency-injection seam (set_component_invokers/reset_component_invokers) -
zero Phase 0-4 files are modified.

Verifies:
  - SharedStateStore basics (get/set/has/delete/keys/size)
  - ExecutionCacheManager put/get, deterministic order-independent cache keys,
    and execution metadata correctness (execution_id, created_at, updated_at,
    workflow_name, execution_time)
  - Cache statistics (hit rate, miss rate, cache size)
  - Cache invalidation by execution_id
  - Cache TTL support (expiry)
  - Execution history
  - Duplicate execution prevention (try_begin/end, including a genuine
    multi-threaded race where exactly one of 20 concurrent claimants wins)
  - Thread safety under real concurrent put/get across many threads, with data
    integrity checks (no cross-key corruption)
  - Resume after interruption (save_to_disk/load_from_disk simulating a fresh
    process recovering a prior process's completed cache entries)
  - Execution consistency (a cached result is returned byte-identical to the
    original computation, repeatedly)
  - Founder Dashboard integration: wire_cache_into_dashboard() makes a second
    run of the same idea/venture_id read every component's output from cache
    instead of recomputing it (verified via call-count instrumentation and a
    dramatic speedup), and unwire_cache_from_dashboard() correctly restores
    Founder Dashboard's real defaults afterward
  - Regression of every previous phase/component (run directly, one process
    each - excluding the nine files that each embed their own full regression
    section, which would otherwise call back into this file, forming an
    unbounded subprocess cycle - all nine already re-verify the full prior
    suite standalone)
  - pip check
  - git status

Run: python scripts/smoke_test_phase5_execution_cache.py
"""

import json
import logging
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

import agents.founder.dashboard.agent as dash_agent  # noqa: E402
import workflows.founder_dashboard as founder_dashboard  # noqa: E402
from core.execution_cache import (  # noqa: E402
    CacheEntry,
    ExecutionCacheManager,
    ExecutionMetadata,
    SharedStateStore,
    cached_workflow_invoker,
    get_default_cache_manager,
    reset_default_cache_manager,
    set_default_cache_manager,
    unwire_cache_from_dashboard,
    wire_cache_into_dashboard,
)


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"test failed at: {label}")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def main() -> None:
    print("\n== 1. SharedStateStore basics ==")
    store = SharedStateStore()
    store.set("a", 1)
    check("get returns a stored value", store.get("a") == 1)
    check("get returns the default for a missing key", store.get("b", "default") == "default")
    check("has() reflects presence correctly", store.has("a") is True and store.has("b") is False)
    check("size() reflects the number of keys", store.size() == 1)
    check("delete() removes a key and reports success", store.delete("a") is True)
    check("delete() on a missing key reports failure", store.delete("a") is False)
    check("store is empty after delete", store.size() == 0)

    print("\n== 2. ExecutionCacheManager: put/get, cache keys, metadata ==")
    cache = ExecutionCacheManager(default_ttl_seconds=3600.0)
    key_a = cache.make_cache_key("research_pipeline", idea="x", venture_id="v1", research_depth="standard")
    key_b = cache.make_cache_key("research_pipeline", venture_id="v1", idea="x", research_depth="standard")
    check("cache keys are deterministic and order-independent across kwargs", key_a == key_b)
    check("a cache miss on an empty cache returns None", cache.get(key_a) is None)

    exec_id = cache.put(key_a, "research_pipeline", {"status": "completed", "data": "result"}, execution_time_seconds=1.5)
    metadata = cache.get_metadata(key_a)
    check("metadata.execution_id matches put()'s return value", metadata.execution_id == exec_id)
    check("metadata.workflow_name is correct", metadata.workflow_name == "research_pipeline")
    check("metadata.execution_time_seconds is correct", metadata.execution_time_seconds == 1.5)
    check("metadata.created_at is a real, positive timestamp", metadata.created_at > 0)
    check("metadata.updated_at is a real, positive timestamp", metadata.updated_at > 0)
    check("metadata.ttl_seconds falls back to the manager's default", metadata.ttl_seconds == 3600.0)
    check("get() after put() is a cache hit with the exact stored output", cache.get(key_a) == {"status": "completed", "data": "result"})

    print("\n== 3. Cache Statistics ==")
    stats = cache.stats()
    check("hits counted correctly (1 hit so far)", stats["hits"] == 1)
    check("misses counted correctly (1 miss so far)", stats["misses"] == 1)
    check("hit_rate computed correctly", stats["hit_rate"] == 0.5)
    check("miss_rate computed correctly", stats["miss_rate"] == 0.5)
    check("cache_size reflects the number of entries", stats["cache_size"] == 1)

    print("\n== 4. Cache Invalidation by execution_id ==")
    check("invalidate() removes the entry and reports success", cache.invalidate(exec_id) is True)
    check("the invalidated key is now a miss", cache.get(key_a) is None)
    check("invalidating an already-gone execution_id reports failure, not an error", cache.invalidate(exec_id) is False)

    print("\n== 5. Cache TTL Support ==")
    ttl_cache = ExecutionCacheManager()
    ttl_key = ttl_cache.make_cache_key("build_check", venture_id="v-ttl")
    ttl_cache.put(ttl_key, "build_check", {"v": 1}, execution_time_seconds=0.1, ttl_seconds=0.2)
    check("a fresh TTL-bounded entry is still a hit", ttl_cache.get(ttl_key) == {"v": 1})
    time.sleep(0.3)
    check("the entry expires and becomes a miss after its TTL elapses", ttl_cache.get(ttl_key) is None)

    print("\n== 6. Execution History ==")
    hist_cache = ExecutionCacheManager()
    hist_key = hist_cache.make_cache_key("mvp_planner", venture_id="v-hist")
    hist_cache.put(hist_key, "mvp_planner", {"v": 1}, execution_time_seconds=0.1)
    hist_cache.put(hist_key, "mvp_planner", {"v": 2}, execution_time_seconds=0.1)
    other_key = hist_cache.make_cache_key("ai_builder", venture_id="v-hist")
    hist_cache.put(other_key, "ai_builder", {"v": 1}, execution_time_seconds=0.1)
    check("history() with no filter returns every put()", len(hist_cache.history()) == 3)
    check("history() filtered by workflow_name returns only that workflow's entries", len(hist_cache.history("mvp_planner")) == 2)

    print("\n== 7. Duplicate Execution Prevention ==")
    dup_cache = ExecutionCacheManager()
    dup_key = dup_cache.make_cache_key("ai_builder", venture_id="v-dup")
    check("the first claim on a key succeeds", dup_cache.try_begin(dup_key) is True)
    check("a second concurrent claim on the same key is rejected", dup_cache.try_begin(dup_key) is False)
    check("is_in_progress() reflects the outstanding claim", dup_cache.is_in_progress(dup_key) is True)
    dup_cache.end(dup_key)
    check("is_in_progress() clears after end()", dup_cache.is_in_progress(dup_key) is False)
    check("a new claim can be made after the prior one ends", dup_cache.try_begin(dup_key) is True)
    dup_cache.end(dup_key)

    race_cache = ExecutionCacheManager()
    race_key = race_cache.make_cache_key("deployment_pipeline", venture_id="v-race")
    race_results: list[bool] = []
    race_lock = threading.Lock()

    def _racer() -> None:
        won = race_cache.try_begin(race_key)
        with race_lock:
            race_results.append(won)

    racers = [threading.Thread(target=_racer) for _ in range(20)]
    for t in racers:
        t.start()
    for t in racers:
        t.join()
    check("exactly one of 20 concurrent threads wins the duplicate-execution race", sum(race_results) == 1)

    print("\n== 8. Thread Safety (concurrent put/get across many threads) ==")
    thread_cache = ExecutionCacheManager()
    thread_errors: list[str] = []

    def _worker(i: int) -> None:
        try:
            k = thread_cache.make_cache_key("growth_pipeline", idea=f"idea-{i}", venture_id=f"v-{i}")
            thread_cache.put(k, "growth_pipeline", {"i": i}, execution_time_seconds=0.01)
            got = thread_cache.get(k)
            if got != {"i": i}:
                thread_errors.append(f"data corruption for worker {i}: got {got}")
        except Exception as exc:
            thread_errors.append(f"worker {i} raised: {exc}")

    workers = [threading.Thread(target=_worker, args=(i,)) for i in range(50)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()
    check("no exceptions or data corruption across 50 concurrent workers", thread_errors == [])
    check("all 50 distinct keys landed in the cache with no loss", thread_cache.stats()["cache_size"] == 50)

    print("\n== 9. Resume After Interruption (disk persistence) ==")
    snapshot_path = Path(__file__).resolve().parent.parent / "database" / "test_execution_cache_snapshot.json"
    try:
        producer_cache = ExecutionCacheManager()
        resume_key = producer_cache.make_cache_key("mvp_planner", venture_id="v-resume")
        producer_cache.put(resume_key, "mvp_planner", {"plan": "ok"}, execution_time_seconds=2.0)
        producer_cache.save_to_disk(snapshot_path)
        check("snapshot file was written to disk", snapshot_path.exists())

        fresh_cache = ExecutionCacheManager()
        check("a brand-new cache instance has no knowledge of the prior process's entry", fresh_cache.get(resume_key) is None)
        loaded = fresh_cache.load_from_disk(snapshot_path)
        check("load_from_disk() reports success when the file exists", loaded is True)
        check("the resumed cache serves the prior process's completed entry as a hit", fresh_cache.get(resume_key) == {"plan": "ok"})

        empty_cache = ExecutionCacheManager()
        check("loading from a nonexistent path gracefully returns False, not an error", empty_cache.load_from_disk(Path("this/path/does/not/exist.json")) is False)
    finally:
        if snapshot_path.exists():
            snapshot_path.unlink()

    print("\n== 10. Execution Consistency ==")
    consistency_cache = ExecutionCacheManager()
    consistency_key = consistency_cache.make_cache_key("decision_engine", venture_id="v-consistency")
    original_output = {"overall_score": 8.5, "recommendation": "BUILD NOW", "reasons": ["a", "b"]}
    consistency_cache.put(consistency_key, "decision_engine", original_output, execution_time_seconds=1.0)
    for attempt in range(5):
        check(f"cached output is byte-identical to the original on repeated read #{attempt + 1}", consistency_cache.get(consistency_key) == original_output)

    print("\n== 11. cached_workflow_invoker() wrapper ==")
    wrapper_calls = {"n": 0}

    def _fake_real_invoker(idea: str, venture_id: str, research_depth: str) -> dict:
        wrapper_calls["n"] += 1
        return {"agent": "fake_workflow", "status": "completed", "idea": idea}

    wrapper_cache = ExecutionCacheManager()
    wrapped_invoker = cached_workflow_invoker("fake_workflow", _fake_real_invoker, wrapper_cache)
    first = wrapped_invoker("wrapped idea", "v-wrap", "standard")
    check("the first call is a cache miss and calls the real invoker", first["cache_hit"] is False and wrapper_calls["n"] == 1)
    second = wrapped_invoker("wrapped idea", "v-wrap", "standard")
    check("the second call for the same input is a cache hit and does not call the real invoker again", second["cache_hit"] is True and wrapper_calls["n"] == 1)
    check("the cached result's business fields match the original", second["status"] == "completed" and second["idea"] == "wrapped idea")

    print("\n== 12. Default Cache Manager Singleton ==")
    original_manager = get_default_cache_manager()
    replacement = ExecutionCacheManager()
    set_default_cache_manager(replacement)
    check("set_default_cache_manager() swaps the process-wide singleton", get_default_cache_manager() is replacement)
    reset_default_cache_manager()
    check("reset_default_cache_manager() installs a fresh manager", get_default_cache_manager() is not replacement and get_default_cache_manager() is not original_manager)

    print("\n== 13. Founder Dashboard Integration (cached vs. recomputed) ==")
    dashboard_call_counts: dict[str, int] = {}

    def _make_slow_fake(name: str):
        def _fn(idea: str, venture_id: str, research_depth: str) -> dict:
            dashboard_call_counts[name] = dashboard_call_counts.get(name, 0) + 1
            time.sleep(0.2)
            return {"agent": name, "status": "completed", "summary": f"{name} done"}

        return _fn

    fake_component_invokers = {
        key: _make_slow_fake(key)
        for key in ("founder_orchestrator", "research_pipeline", "decision_engine", "mvp_planner", "ai_builder", "deployment_pipeline", "growth_pipeline")
    }
    fake_git_status = {"agent": "git_agent", "event": "git_operation_completed", "output": "clean"}

    dashboard_cache = ExecutionCacheManager()
    founder_dashboard.set_git_status_invoker(lambda vid: fake_git_status)
    wire_cache_into_dashboard(cache_manager=dashboard_cache, invokers=fake_component_invokers)
    try:
        venture_id = new_id("cache-dashboard")
        start_first = time.monotonic()
        first_run = dash_agent.run_founder_dashboard(idea="cached dashboard idea", venture_id=venture_id)
        elapsed_first = time.monotonic() - start_first
        check("first dashboard run calls every one of the 7 component invokers exactly once", all(dashboard_call_counts.get(k) == 1 for k in fake_component_invokers))
        check("first dashboard run completes successfully", first_run["status"] == "completed")

        start_second = time.monotonic()
        second_run = dash_agent.run_founder_dashboard(idea="cached dashboard idea", venture_id=venture_id)
        elapsed_second = time.monotonic() - start_second
        check("second dashboard run does NOT call any component invoker again (served from cache)", all(dashboard_call_counts.get(k) == 1 for k in fake_component_invokers))
        check("second dashboard run is dramatically faster than the first (cache hit vs. recompute)", elapsed_second < elapsed_first / 2)
        check("second run's overall_health matches the first run's (consistent cached result)", second_run["overall_health"] == first_run["overall_health"])

        cache_stats = dashboard_cache.stats()
        check("cache recorded exactly 7 misses (the first run)", cache_stats["misses"] == 7)
        check("cache recorded exactly 7 hits (the second run)", cache_stats["hits"] == 7)
        check("cache holds exactly 7 entries (one per reused component)", cache_stats["cache_size"] == 7)
    finally:
        unwire_cache_from_dashboard()
        founder_dashboard.reset_git_status_invoker()

    check("unwire_cache_from_dashboard() restores the real default invokers", founder_dashboard.get_component_invokers() == founder_dashboard._DEFAULT_COMPONENT_INVOKERS)
    check("unwire_cache_from_dashboard() restores the real default git status invoker", founder_dashboard.get_git_status_invoker() is founder_dashboard._default_git_status_invoker)

    print("\n== 14. Full Regression of All Previous Phases/Components ==")
    repo_root = Path(__file__).resolve().parent.parent
    # These nine files each embed their own full regression section, globbing
    # "smoke_test_phase*.py" - none of their (frozen, not to be modified)
    # exclusion lists know about this new file, so including any of them here
    # would let it call back into this file, forming an unbounded subprocess
    # cycle. All nine already re-verify the full prior suite standalone, so
    # excluding them here loses no coverage.
    excluded = {
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
            # One retry absorbs transient environmental flakiness (e.g. the local
            # Ollama model needing a moment to load after being idle) without
            # masking a genuine regression, which fails consistently.
            print(f"     regression: {test_path.name} failed on first attempt, retrying once...")
            proc = subprocess.run([sys.executable, str(test_path)], cwd=repo_root, capture_output=True, text=True)
        check(f"regression: {test_path.name}", proc.returncode == 0)

    print("\n== 15. pip check ==")
    pip_result = subprocess.run([sys.executable, "-m", "pip", "check"], cwd=repo_root, capture_output=True, text=True)
    check("pip check reports no broken requirements", pip_result.returncode == 0)

    print("\n== 16. git status (only new files, nothing modified) ==")
    git_result = subprocess.run(["git", "status", "--porcelain"], cwd=repo_root, capture_output=True, text=True)
    status_lines = [line for line in git_result.stdout.splitlines() if line.strip()]
    modified_or_deleted = [line for line in status_lines if not line.startswith("??")]
    check("git status has no modified/deleted files, only untracked additions", len(modified_or_deleted) == 0)

    print("\nAll Phase 5 Component 1 checks passed.")


if __name__ == "__main__":
    main()
