from collections.abc import Callable
from typing import Any


class ValidationSeverity:
    ERROR = "error"
    WARNING = "warning"


def validate_required_env_vars(env_vars: list[str], provided: dict[str, str]) -> list[dict[str, Any]]:
    issues = []
    for key in env_vars:
        if not provided.get(key):
            issues.append({"severity": ValidationSeverity.ERROR, "check": "env_var", "message": f"missing required env var: {key}"})
    return issues


def validate_ports(ports: list[int]) -> list[dict[str, Any]]:
    issues = []
    seen: set[int] = set()
    for port in ports:
        if not (1 <= port <= 65535):
            issues.append({"severity": ValidationSeverity.ERROR, "check": "port", "message": f"invalid port: {port}"})
        elif port in seen:
            issues.append({"severity": ValidationSeverity.ERROR, "check": "port", "message": f"duplicate port: {port}"})
        seen.add(port)
    return issues


def validate_dependencies(requirements_text: str) -> list[dict[str, Any]]:
    issues = []
    lines = [line.strip() for line in requirements_text.splitlines() if line.strip() and not line.strip().startswith("#")]
    if not lines:
        issues.append({"severity": ValidationSeverity.WARNING, "check": "dependencies", "message": "no dependencies declared"})
    return issues


def validate_health_endpoint(health_path: str) -> list[dict[str, Any]]:
    issues = []
    if not health_path.startswith("/"):
        issues.append({"severity": ValidationSeverity.ERROR, "check": "health_endpoint", "message": "health endpoint path must start with '/'"})
    return issues


def _default_docker_build_check(dockerfile_text: str) -> list[dict[str, Any]]:
    """Structural validation, not a real `docker build` - this environment
    has no guarantee a Docker daemon is installed/running (matches every
    prior "no live service in this sandbox" finding this whole session:
    Playwright, Crawl4AI, SearXNG). See get_docker_build_checker()/
    set_docker_build_checker() for how a caller with a real Docker daemon can
    swap in genuine validation (e.g. via the Phase 3 Docker infra agent's own
    public run_docker_operation("docker_build", ...) API) without this
    module needing to import or depend on Phase 3 at all.
    """
    issues = []
    if "FROM " not in dockerfile_text:
        issues.append({"severity": ValidationSeverity.ERROR, "check": "docker_build", "message": "Dockerfile missing FROM instruction"})
    if "EXPOSE" not in dockerfile_text:
        issues.append({"severity": ValidationSeverity.WARNING, "check": "docker_build", "message": "Dockerfile does not EXPOSE a port"})
    if "CMD" not in dockerfile_text and "ENTRYPOINT" not in dockerfile_text:
        issues.append({"severity": ValidationSeverity.ERROR, "check": "docker_build", "message": "Dockerfile missing CMD or ENTRYPOINT"})
    return issues


DockerBuildChecker = Callable[[str], list[dict[str, Any]]]

_docker_build_checker: DockerBuildChecker = _default_docker_build_check


def get_docker_build_checker() -> DockerBuildChecker:
    return _docker_build_checker


def set_docker_build_checker(fn: DockerBuildChecker) -> None:
    global _docker_build_checker
    _docker_build_checker = fn


def reset_docker_build_checker() -> None:
    global _docker_build_checker
    _docker_build_checker = _default_docker_build_check


def validate_deployment(
    *,
    env_vars: list[str],
    provided_env: dict[str, str],
    ports: list[int],
    requirements_text: str,
    health_path: str,
    dockerfile_text: str,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    issues += validate_required_env_vars(env_vars, provided_env)
    issues += validate_ports(ports)
    issues += validate_dependencies(requirements_text)
    issues += validate_health_endpoint(health_path)
    issues += get_docker_build_checker()(dockerfile_text)

    error_count = sum(1 for i in issues if i["severity"] == ValidationSeverity.ERROR)
    return {
        "valid": error_count == 0,
        "issues": issues,
        "error_count": error_count,
        "warning_count": len(issues) - error_count,
    }
