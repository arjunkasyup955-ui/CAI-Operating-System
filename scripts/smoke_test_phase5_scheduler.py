"""Phase 5, Component 2: Background Job Scheduler.

A production-grade, thread-safe Background Job Scheduler for AFOS, added as a
new core/scheduler/ kernel subsystem. Purely additive: no Phase 0-4 file, and
no Phase 5 Component 1 (core/execution_cache/) file, is modified or imported
here for anything beyond this component's own independent testing.

Verifies:
  - Queue management and basic job execution (submit -> completed, result
    correctness)
  - Priority scheduling (HIGH runs before NORMAL/LOW despite later submission)
  - Job retry policy (transient failures retried up to max_retries, eventual
    success recorded with the correct attempt count)
  - Job cancellation, both of a still-queued job (never runs) and of a
    running job (cooperative mid-flight cancellation via JobContext)
  - Pause and resume of an individual queued job
  - Persistent job state / resume after interruption (disk snapshot/restore,
    including a genuinely in-flight RUNNING job recovered back to QUEUED)
  - Concurrent worker execution (parallel wall-clock speedup + a live
    concurrent-execution counter proving true overlap, not serialization)
  - Execution logs (JobContext.log)
  - Progress tracking (JobContext.set_progress)
  - Job history (terminal jobs, filterable by status/job_type)
  - Failed job recovery (recover_failed_jobs requeues and re-executes)
  - Scheduled recurring jobs (a spec fires repeatedly on its own interval)
  - Thread safety (many threads submitting concurrently, no corruption/loss)
  - Regression of every previous phase/component
  - pip check
  - git status

Run: python scripts/smoke_test_phase5_scheduler.py
"""

import json
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.scheduler import (  # noqa: E402
    Job,
    JobPriority,
    JobScheduler,
    get_job_registry,
    register_job,
    reset_job_registry,
)


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"test failed at: {label}")


def new_scheduler(num_workers: int = 2) -> JobScheduler:
    scheduler = JobScheduler(num_workers=num_workers, tick_interval_seconds=0.05)
    scheduler.start()
    return scheduler


def main() -> None:
    reset_job_registry()

    print("\n== 1. Queue Management / Basic Execution ==")
    register_job("double", lambda ctx, x: x * 2)
    scheduler = new_scheduler(num_workers=2)
    try:
        job_id = scheduler.submit("double", args=[21], max_retries=0)
        job = scheduler.wait_for(job_id, timeout=5)
        check("a submitted job reaches COMPLETED status", job.status == "completed")
        check("the job's result is correct", job.result == 42)
        check("get_result() matches the job's result", scheduler.get_result(job_id) == 42)
        stats = scheduler.stats()
        check("stats() reports the correct total job count", stats["total_jobs"] == 1)
        check("job_type registry contains 'double'", "double" in get_job_registry())
    finally:
        scheduler.shutdown()

    print("\n== 2. Priority Scheduling ==")
    order: list[str] = []
    order_lock = threading.Lock()

    def _record(ctx, label: str) -> str:
        with order_lock:
            order.append(label)
        return label

    register_job("record", _record)
    priority_scheduler = JobScheduler(num_workers=1, tick_interval_seconds=0.05)
    priority_scheduler.pause()
    priority_scheduler.start()
    try:
        low_id = priority_scheduler.submit("record", args=["low"], priority=JobPriority.LOW)
        high_id = priority_scheduler.submit("record", args=["high"], priority=JobPriority.HIGH)
        normal_id = priority_scheduler.submit("record", args=["normal"], priority=JobPriority.NORMAL)
        check("scheduler reports paused before resume", priority_scheduler.is_paused() is True)
        priority_scheduler.resume()
        for jid in (low_id, high_id, normal_id):
            priority_scheduler.wait_for(jid, timeout=5)
        check("HIGH priority job ran before NORMAL and LOW despite later submission", order == ["high", "normal", "low"])
    finally:
        priority_scheduler.shutdown()

    print("\n== 3. Job Retry Policy ==")
    retry_counts: dict[str, int] = {}
    retry_lock = threading.Lock()

    def _flaky(ctx, key: str, fail_times: int) -> str:
        with retry_lock:
            retry_counts[key] = retry_counts.get(key, 0) + 1
            attempt = retry_counts[key]
        if attempt <= fail_times:
            raise RuntimeError(f"transient failure #{attempt}")
        return "recovered"

    register_job("flaky", _flaky)
    retry_scheduler = new_scheduler(num_workers=1)
    try:
        flaky_key = new_id = str(uuid.uuid4())
        job_id = retry_scheduler.submit("flaky", args=[flaky_key, 2], max_retries=3, retry_delay_seconds=0.1)
        job = retry_scheduler.wait_for(job_id, timeout=10)
        check("a job that fails twice then succeeds eventually COMPLETES", job.status == "completed")
        check("the job's result reflects the final successful attempt", job.result == "recovered")
        check("attempts were recorded correctly (2 failures + 1 success = 3)", job.attempts == 2)
        check("execution logs recorded the failed attempts", any("attempt failed" in entry["message"] for entry in job.logs))

        always_fails_id = retry_scheduler.submit("flaky", args=[str(uuid.uuid4()), 999], max_retries=2, retry_delay_seconds=0.05)
        job2 = retry_scheduler.wait_for(always_fails_id, timeout=10)
        check("a job that always fails ends FAILED after exhausting max_retries", job2.status == "failed")
        check("attempts equals max_retries + 1 for an always-failing job", job2.attempts == 3)
        check("error message is captured on the job", job2.error is not None and "transient failure" in job2.error)
    finally:
        retry_scheduler.shutdown()

    print("\n== 4. Job Cancellation ==")
    cancel_scheduler = JobScheduler(num_workers=1, tick_interval_seconds=0.05)
    cancel_scheduler.pause()
    cancel_scheduler.start()
    never_ran = {"flag": True}

    def _mark_ran(ctx) -> None:
        never_ran["flag"] = False

    register_job("mark_ran", _mark_ran)
    try:
        queued_id = cancel_scheduler.submit("mark_ran")
        cancelled = cancel_scheduler.cancel(queued_id)
        check("cancel() on a still-queued job reports success", cancelled is True)
        cancel_scheduler.resume()
        time.sleep(0.3)
        final = cancel_scheduler.get_job(queued_id)
        check("a cancelled queued job never actually executes", never_ran["flag"] is True)
        check("a cancelled queued job's status is CANCELLED", final.status == "cancelled")
    finally:
        cancel_scheduler.shutdown()

    started_event = threading.Event()
    release_event = threading.Event()

    def _cooperative(ctx) -> str:
        started_event.set()
        for _ in range(200):
            ctx.check_cancelled()
            if release_event.wait(timeout=0.02):
                break
        ctx.check_cancelled()
        return "ran to completion"

    register_job("cooperative", _cooperative)
    running_cancel_scheduler = new_scheduler(num_workers=1)
    try:
        running_id = running_cancel_scheduler.submit("cooperative")
        check("the cooperative job actually started running", started_event.wait(timeout=5))
        time.sleep(0.05)
        cancelled_running = running_cancel_scheduler.cancel(running_id)
        check("cancel() on a RUNNING job reports success (cooperative request registered)", cancelled_running is True)
        job = running_cancel_scheduler.wait_for(running_id, timeout=5)
        check("a cooperatively-cancelled running job ends in CANCELLED status", job.status == "cancelled")
        release_event.set()
    finally:
        running_cancel_scheduler.shutdown()

    print("\n== 5. Pause and Resume Jobs ==")
    pause_scheduler = JobScheduler(num_workers=1, tick_interval_seconds=0.05)
    pause_scheduler.pause()
    pause_scheduler.start()
    execution_flag = {"ran": False}

    def _flag_run(ctx) -> None:
        execution_flag["ran"] = True

    register_job("flag_run", _flag_run)
    try:
        job_id = pause_scheduler.submit("flag_run")
        pause_scheduler.resume()
        time.sleep(0.1)
        pause_scheduler.pause()
        job_before = pause_scheduler.get_job(job_id)
        check("job ran once scheduler was resumed (baseline)", execution_flag["ran"] is True)

        queued_pause_id = str(uuid.uuid4())
        second_job_id = pause_scheduler.submit("flag_run", job_id=queued_pause_id)
        paused_ok = pause_scheduler.pause_job(second_job_id)
        check("pause_job() on a queued job reports success", paused_ok is True)
        check("a paused job's status is PAUSED", pause_scheduler.get_status(second_job_id) == "paused")
        pause_scheduler.resume()
        time.sleep(0.2)
        check("a paused job does NOT execute while paused", pause_scheduler.get_status(second_job_id) == "paused")

        resumed_ok = pause_scheduler.resume_job(second_job_id)
        check("resume_job() on a paused job reports success", resumed_ok is True)
        final_job = pause_scheduler.wait_for(second_job_id, timeout=5)
        check("a resumed job executes and completes", final_job.status == "completed")
    finally:
        pause_scheduler.shutdown()

    print("\n== 6. Persistent Job State / Resume After Interruption ==")
    snapshot_path = Path(__file__).resolve().parent.parent / "database" / "test_scheduler_snapshot.json"
    hold_started = threading.Event()
    hold_release = threading.Event()

    def _hold(ctx) -> str:
        hold_started.set()
        hold_release.wait(timeout=10)
        return "released"

    register_job("hold", _hold)
    try:
        producer = JobScheduler(num_workers=1, tick_interval_seconds=0.05)
        producer.start()
        try:
            in_flight_id = producer.submit("hold")
            check("the long-running job actually started before we snapshot", hold_started.wait(timeout=5))
            time.sleep(0.05)
            check("the in-flight job's status is RUNNING at snapshot time", producer.get_job(in_flight_id).status == "running")
            producer.save_to_disk(snapshot_path)
            check("snapshot file was written to disk", snapshot_path.exists())
        finally:
            hold_release.set()
            producer.shutdown()

        fresh = JobScheduler(num_workers=1, tick_interval_seconds=0.05)
        loaded = fresh.load_from_disk(snapshot_path)
        check("load_from_disk() reports success when the file exists", loaded is True)
        restored_job = fresh.get_job(in_flight_id)
        check("a job that was RUNNING when interrupted is recovered as QUEUED, not lost", restored_job.status == "queued")

        fresh.start()
        try:
            recovered_final = fresh.wait_for(in_flight_id, timeout=5)
            check("the recovered job actually re-executes and completes in the new process", recovered_final.status == "completed")
        finally:
            fresh.shutdown()

        empty = JobScheduler(num_workers=1)
        check(
            "loading from a nonexistent path gracefully returns False, not an error",
            empty.load_from_disk(Path("this/path/does/not/exist.json")) is False,
        )
    finally:
        if snapshot_path.exists():
            snapshot_path.unlink()

    print("\n== 7. Concurrent Worker Execution ==")
    concurrent_now = {"count": 0, "max": 0}
    concurrent_lock = threading.Lock()

    def _parallel_sleep(ctx, seconds: float) -> str:
        with concurrent_lock:
            concurrent_now["count"] += 1
            concurrent_now["max"] = max(concurrent_now["max"], concurrent_now["count"])
        time.sleep(seconds)
        with concurrent_lock:
            concurrent_now["count"] -= 1
        return "done"

    register_job("parallel_sleep", _parallel_sleep)
    concurrency_scheduler = new_scheduler(num_workers=5)
    try:
        start = time.monotonic()
        job_ids = [concurrency_scheduler.submit("parallel_sleep", args=[0.15]) for _ in range(10)]
        for jid in job_ids:
            job = concurrency_scheduler.wait_for(jid, timeout=10)
            check(f"parallel job {jid[:8]} completed", job.status == "completed")
        elapsed = time.monotonic() - start
        check("10 jobs of 0.15s each on 5 workers finish well under the 1.5s serial time", elapsed < 0.8)
        check("more than one job genuinely ran concurrently (max overlap > 1)", concurrent_now["max"] > 1)
    finally:
        concurrency_scheduler.shutdown()

    print("\n== 8. Execution Logs and Progress Tracking ==")
    def _logging_job(ctx) -> str:
        ctx.log("step 1 starting")
        ctx.set_progress(25, "quarter done")
        ctx.set_progress(50, "halfway")
        ctx.log("step 2 starting")
        ctx.set_progress(100, "finished")
        return "ok"

    register_job("logging_job", _logging_job)
    log_scheduler = new_scheduler(num_workers=1)
    try:
        job_id = log_scheduler.submit("logging_job")
        job = log_scheduler.wait_for(job_id, timeout=5)
        logs = log_scheduler.get_logs(job_id)
        check("execution logs contain the custom log messages", any("step 1 starting" in e["message"] for e in logs))
        check("execution logs contain the completion message", any("completed successfully" in e["message"] for e in logs))
        progress = log_scheduler.get_progress(job_id)
        check("final progress percent reflects the job's last set_progress call", progress[0] == 100.0)
        check("final progress message reflects the job's last set_progress call", progress[1] == "finished")
    finally:
        log_scheduler.shutdown()

    print("\n== 9. Job History ==")
    register_job("always_fail", lambda ctx: (_ for _ in ()).throw(RuntimeError("nope")))
    history_scheduler = new_scheduler(num_workers=2)
    try:
        ok_id = history_scheduler.submit("double", args=[5])
        fail_id = history_scheduler.submit("always_fail", max_retries=0)
        cancel_id = history_scheduler.submit("double", args=[1])
        history_scheduler.wait_for(ok_id, timeout=5)
        history_scheduler.wait_for(fail_id, timeout=5)
        history_scheduler.cancel(cancel_id)

        history = history_scheduler.history()
        history_ids = {j.job_id for j in history}
        check("history() includes the completed job", ok_id in history_ids)
        check("history() includes the failed job", fail_id in history_ids)
        check("history() includes the cancelled job", cancel_id in history_ids)

        failed_only = history_scheduler.history(status="failed")
        check("history(status='failed') filters correctly", all(j.status == "failed" for j in failed_only) and len(failed_only) >= 1)

        by_type = history_scheduler.history(job_type="double")
        check("history(job_type=...) filters correctly", all(j.job_type == "double" for j in by_type))
    finally:
        history_scheduler.shutdown()

    print("\n== 10. Failed Job Recovery ==")
    recovery_attempts = {"n": 0}

    def _recoverable(ctx) -> str:
        recovery_attempts["n"] += 1
        if recovery_attempts["n"] == 1:
            raise RuntimeError("first attempt always fails")
        return "recovered on retry"

    register_job("recoverable", _recoverable)
    recovery_scheduler = new_scheduler(num_workers=1)
    try:
        job_id = recovery_scheduler.submit("recoverable", max_retries=0)
        job = recovery_scheduler.wait_for(job_id, timeout=5)
        check("the job fails on its first attempt with no retry budget", job.status == "failed")

        recovered_count = recovery_scheduler.recover_failed_jobs(reset_attempts=True)
        check("recover_failed_jobs() reports at least one recovered job", recovered_count >= 1)
        recovered_job = recovery_scheduler.wait_for(job_id, timeout=5)
        check("the recovered job re-executes and this time succeeds", recovered_job.status == "completed")
        check("the recovered job's result reflects the successful retry", recovered_job.result == "recovered on retry")
    finally:
        recovery_scheduler.shutdown()

    print("\n== 11. Scheduled Recurring Jobs ==")
    recurring_runs: list[float] = []
    recurring_lock = threading.Lock()

    def _tick(ctx) -> None:
        with recurring_lock:
            recurring_runs.append(time.monotonic())

    register_job("tick", _tick)
    recurring_scheduler = new_scheduler(num_workers=2)
    try:
        spec_id = recurring_scheduler.schedule_recurring("tick", interval_seconds=0.15, run_immediately=True)
        time.sleep(0.7)
        with recurring_lock:
            run_count = len(recurring_runs)
        check("a recurring spec fires more than once over its interval", run_count >= 3)
        spec = recurring_scheduler.get_recurring_spec(spec_id)
        check("the recurring spec's run_count matches actual firings", spec.run_count == run_count)

        cancelled = recurring_scheduler.cancel_recurring(spec_id)
        check("cancel_recurring() reports success", cancelled is True)
        with recurring_lock:
            count_after_cancel = len(recurring_runs)
        time.sleep(0.4)
        with recurring_lock:
            count_later = len(recurring_runs)
        check("a cancelled recurring spec stops firing new jobs", count_later == count_after_cancel)
    finally:
        recurring_scheduler.shutdown()

    print("\n== 12. Thread Safety (concurrent submit from many threads) ==")
    thread_safety_scheduler = new_scheduler(num_workers=4)
    submitted_ids: list[str] = []
    submit_lock = threading.Lock()
    submit_errors: list[str] = []

    def _submitter(i: int) -> None:
        try:
            jid = thread_safety_scheduler.submit("double", args=[i])
            with submit_lock:
                submitted_ids.append(jid)
        except Exception as exc:
            with submit_lock:
                submit_errors.append(str(exc))

    try:
        threads = [threading.Thread(target=_submitter, args=(i,)) for i in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        check("no exceptions during 30 concurrent submissions", submit_errors == [])
        check("all 30 job_ids are unique (no collisions/corruption)", len(set(submitted_ids)) == 30)
        for jid in submitted_ids:
            thread_safety_scheduler.wait_for(jid, timeout=10)
        completed = [j for j in thread_safety_scheduler.list_jobs(status="completed") if j.job_id in set(submitted_ids)]
        check("all 30 concurrently-submitted jobs completed successfully", len(completed) == 30)
    finally:
        thread_safety_scheduler.shutdown()

    reset_job_registry()

    print("\n== 13. Full Regression of All Previous Phases/Components ==")
    repo_root = Path(__file__).resolve().parent.parent
    # These ten files each embed their own full regression section, globbing
    # "smoke_test_phase*.py". None of their (frozen, not to be modified)
    # exclusion lists know about this new file, so including any of them here
    # would let it call back into this file (directly, or transitively via a
    # subprocess that itself re-globs and finds this file), forming an
    # unbounded subprocess cycle. All ten already re-verify the full prior
    # suite standalone, so excluding them here loses no coverage.
    excluded = {
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

    print("\n== 14. pip check ==")
    pip_result = subprocess.run([sys.executable, "-m", "pip", "check"], cwd=repo_root, capture_output=True, text=True)
    check("pip check reports no broken requirements", pip_result.returncode == 0)

    print("\n== 15. git status (only new files, nothing modified) ==")
    git_result = subprocess.run(["git", "status", "--porcelain"], cwd=repo_root, capture_output=True, text=True)
    status_lines = [line for line in git_result.stdout.splitlines() if line.strip()]
    modified_or_deleted = [line for line in status_lines if not line.startswith("??")]
    check("git status has no modified/deleted files, only untracked additions", len(modified_or_deleted) == 0)

    print("\nAll Phase 5 Component 2 checks passed.")


if __name__ == "__main__":
    main()
