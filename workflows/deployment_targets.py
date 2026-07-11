import logging
import time
from typing import Any

from core.event_bus import AFOSEvent, get_event_bus
from core.execution_cache import ExecutionCacheManager, get_default_cache_manager
from core.scheduler import JobContext, JobPriority, JobScheduler, register_job
from deployment.generator import DeploymentGenerator
from deployment.history import (
    DeploymentRecordStatus,
    generate_rollback_script,
    get_default_deployment_history_store,
)
from deployment.targets import ALL_TARGETS, DeploymentProfile
from deployment.validation import validate_deployment

logger = logging.getLogger("afos.workflows.deployment_targets")

BULK_GENERATION_JOB_TYPE = "deployment_bulk_generation"

_generator = DeploymentGenerator()


def _publish(event_type: str, venture_id: str, target: str, extra: dict[str, Any] | None = None) -> None:
    get_event_bus().publish(
        AFOSEvent(type=f"deployment_target_{event_type}", source_agent="deployment_targets", venture_id=venture_id, payload={"target": target, **(extra or {})})
    )


def generate_deployment_artifacts(
    venture_id: str,
    project_name: str,
    target: str,
    profile: str = DeploymentProfile.PRODUCTION,
    version: str = "v0.1.0",
    port: int = 8000,
    health_path: str = "/health",
    entrypoint: str = "apps/api/main.py",
    env_vars: list[str] | None = None,
    secret_keys: list[str] | None = None,
    provided_env: dict[str, str] | None = None,
    requirements_text: str = "",
    write_output_dir: str | None = None,
    history_store=None,
) -> dict[str, Any]:
    """Generates one target's deployment artifacts, validates them, records
    the result into the shared DeploymentHistoryStore (rollback support) and,
    automatically, into the same core.observability.ExecutionHistoryStore and
    Event Bus Phase 5 Component 4's dashboard already reads from - the same
    zero-modification integration pattern established by every Phase 5
    component since Component 5.
    """
    start = time.monotonic()
    env_vars = env_vars or []
    history_store = history_store or get_default_deployment_history_store()

    artifacts = _generator.generate(
        project_name, target, profile=profile, port=port, health_path=health_path,
        entrypoint=entrypoint, env_vars=env_vars, secret_keys=secret_keys,
    )
    dockerfile_text = artifacts.get("Dockerfile", "FROM python:3.11-slim\nEXPOSE 8000\nCMD [\"python\", \"app.py\"]\n")
    validation = validate_deployment(
        env_vars=env_vars, provided_env=provided_env or {}, ports=[port],
        requirements_text=requirements_text, health_path=health_path, dockerfile_text=dockerfile_text,
    )

    written_paths: list[str] = []
    if write_output_dir:
        written_paths = [str(p) for p in _generator.write_to_disk(artifacts, write_output_dir)]

    status = DeploymentRecordStatus.DEPLOYED if validation["valid"] else DeploymentRecordStatus.FAILED
    record = history_store.record_deployment(
        venture_id=venture_id, target=target, profile=profile, version=version,
        artifacts_summary={"project_name": project_name, "files": list(artifacts.keys()), "validation": validation}, status=status,
    )

    elapsed = time.monotonic() - start
    _publish("generated", venture_id, target, {"deployment_id": record["deployment_id"], "valid": validation["valid"]})

    from core.observability import get_default_execution_history

    get_default_execution_history().record_execution(
        pipeline_name="deployment_targets", venture_id=venture_id, status=status,
        execution_time_seconds=elapsed, confidence_score=None, competitor_count=None,
        metadata={"target": target, "profile": profile, "deployment_id": record["deployment_id"], "valid": validation["valid"]},
        execution_id=record["deployment_id"],
    )
    logger.info("deployment_targets: generated %s artifacts for venture=%s target=%s valid=%s", project_name, venture_id, target, validation["valid"])

    return {
        "target": target, "artifacts": artifacts, "validation": validation, "deployment_record": record,
        "written_paths": written_paths, "execution_time_seconds": round(elapsed, 4),
    }


def generate_all_deployment_artifacts(venture_id: str, project_name: str, profile: str = DeploymentProfile.PRODUCTION, **kwargs: Any) -> dict[str, Any]:
    results = {target: generate_deployment_artifacts(venture_id, project_name, target, profile=profile, **kwargs) for target in ALL_TARGETS}
    all_valid = all(r["validation"]["valid"] for r in results.values())
    return {"results": results, "all_valid": all_valid, "targets": list(ALL_TARGETS)}


def rollback_deployment(venture_id: str, target: str, deployment_id: str, history_store=None) -> dict[str, Any] | None:
    history_store = history_store or get_default_deployment_history_store()
    target_record = history_store.get_deployment(deployment_id)
    if target_record is None:
        return None
    rollback_record = history_store.rollback_to(venture_id, target, deployment_id)
    if rollback_record is None:
        return None

    script = generate_rollback_script(target, target_record.get("artifacts_summary", {}).get("project_name", "app"), target_record["version"])
    _publish("rolled_back", venture_id, target, {"deployment_id": rollback_record["deployment_id"], "rollback_of": deployment_id})

    from core.observability import get_default_execution_history

    get_default_execution_history().record_execution(
        pipeline_name="deployment_targets", venture_id=venture_id, status="rolled_back",
        execution_time_seconds=0.0, confidence_score=None, competitor_count=None,
        metadata={"target": target, "rollback_of": deployment_id}, execution_id=rollback_record["deployment_id"],
    )
    logger.info("deployment_targets: rolled back venture=%s target=%s to deployment=%s", venture_id, target, deployment_id)
    return {"rollback_record": rollback_record, "rollback_script": script}


def compute_deployment_health(venture_id: str | None = None, history_store=None) -> dict[str, Any]:
    """Deterministic health scoring over recent deployment records per
    target - "Health Status" / "Health Checks" for the dashboard.
    """
    history_store = history_store or get_default_deployment_history_store()
    records = history_store.list_deployments(venture_id=venture_id)
    per_target: dict[str, str] = {}
    for target in ALL_TARGETS:
        target_records = [r for r in records if r["target"] == target]
        if not target_records:
            per_target[target] = "unknown"
            continue
        latest = target_records[0]
        if latest["status"] == DeploymentRecordStatus.DEPLOYED:
            per_target[target] = "healthy"
        elif latest["status"] == DeploymentRecordStatus.ROLLED_BACK:
            per_target[target] = "degraded"
        else:
            per_target[target] = "critical"

    healthy_count = sum(1 for v in per_target.values() if v == "healthy")
    known_count = sum(1 for v in per_target.values() if v != "unknown")
    overall_ratio = (healthy_count / known_count) if known_count else 1.0
    overall = "healthy" if overall_ratio >= 0.8 else ("degraded" if overall_ratio >= 0.5 else "critical")
    return {"overall_health": overall, "overall_score": round(overall_ratio, 4), "per_target": per_target}


def get_cached_deployment_generation(cache_manager: ExecutionCacheManager | None = None, ttl_seconds: float | None = 300.0):
    """Wraps generate_deployment_artifacts with Phase 5 Component 1's
    Execution Cache (unmodified) - regenerating identical deployment
    artifacts for the same (venture, project, target, profile) tuple is
    pure/deterministic, so caching avoids redundant validation/IO work.
    """
    manager = cache_manager or get_default_cache_manager()

    def _invoker(venture_id: str, project_name: str, target: str) -> dict[str, Any]:
        cache_key = manager.make_cache_key("deployment_targets", venture_id=venture_id, project_name=project_name, target=target)
        cached = manager.get(cache_key)
        if cached is not None:
            return {**cached, "cache_hit": True}
        start = time.monotonic()
        result = generate_deployment_artifacts(venture_id, project_name, target)
        elapsed = time.monotonic() - start
        manager.put(cache_key, "deployment_targets", result, elapsed, ttl_seconds=ttl_seconds)
        return {**result, "cache_hit": False}

    return _invoker


def _bulk_generation_job_fn(ctx: JobContext, venture_id: str, project_name: str, profile: str = DeploymentProfile.PRODUCTION) -> dict[str, Any]:
    ctx.log(f"bulk deployment generation starting for {project_name!r} (venture={venture_id})")
    results: dict[str, Any] = {}
    for i, target in enumerate(ALL_TARGETS, start=1):
        ctx.check_cancelled()
        ctx.set_progress(round(100.0 * i / len(ALL_TARGETS), 2), f"generating {target}")
        results[target] = generate_deployment_artifacts(venture_id, project_name, target, profile=profile)
        ctx.log(f"generated {target} artifacts (valid={results[target]['validation']['valid']})")
    ctx.set_progress(100.0, "bulk generation complete")
    return {"status": "completed", "targets": list(results.keys()), "all_valid": all(r["validation"]["valid"] for r in results.values())}


def register_bulk_generation_job() -> None:
    register_job(BULK_GENERATION_JOB_TYPE, _bulk_generation_job_fn)


def submit_bulk_generation_job(
    scheduler: JobScheduler, venture_id: str, project_name: str, profile: str = DeploymentProfile.PRODUCTION, priority: int = JobPriority.NORMAL,
) -> str:
    register_bulk_generation_job()
    return scheduler.submit(BULK_GENERATION_JOB_TYPE, kwargs={"venture_id": venture_id, "project_name": project_name, "profile": profile}, priority=priority)
