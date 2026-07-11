from typing import Any

import yaml

from deployment.targets import DeploymentProfile, resolve_profile_defaults


def generate_dockerfile(
    project_name: str,
    entrypoint: str = "apps/api/main.py",
    python_version: str = "3.11",
    port: int = 8000,
    health_path: str = "/health",
    profile: str = DeploymentProfile.PRODUCTION,
) -> str:
    """Pure, deterministic Dockerfile text generation - no LLM, no template
    engine dependency needed for a structure this well-known. Uses the
    stdlib-only urllib-based healthcheck so the image needs no extra curl/wget
    package.
    """
    defaults = resolve_profile_defaults(profile)
    return (
        "# syntax=docker/dockerfile:1\n"
        f"FROM python:{python_version}-slim AS base\n\n"
        "WORKDIR /app\n"
        "ENV PYTHONUNBUFFERED=1 \\\n"
        "    PYTHONDONTWRITEBYTECODE=1 \\\n"
        f"    LOG_LEVEL={defaults['log_level']}\n\n"
        "COPY requirements.txt ./\n"
        "RUN pip install --no-cache-dir -r requirements.txt\n\n"
        "COPY . .\n\n"
        f"EXPOSE {port}\n"
        "HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \\\n"
        f"    CMD python -c \"import urllib.request; urllib.request.urlopen('http://localhost:{port}{health_path}')\"\n\n"
        f'CMD ["python", "{entrypoint}"]\n'
    )


def generate_docker_compose(
    project_name: str,
    port: int = 8000,
    profile: str = DeploymentProfile.PRODUCTION,
    env_vars: list[str] | None = None,
    extra_services: dict[str, Any] | None = None,
) -> str:
    defaults = resolve_profile_defaults(profile)
    environment = {"LOG_LEVEL": defaults["log_level"], "PORT": str(port)}
    for key in env_vars or []:
        environment[key] = f"${{{key}}}"

    cpu_millicores = int(str(defaults["cpu"]).rstrip("m"))
    cpu_cores = round(cpu_millicores / 1000, 3)

    compose: dict[str, Any] = {
        "version": "3.9",
        "services": {
            project_name: {
                "build": ".",
                "ports": [f"{port}:{port}"],
                "environment": environment,
                "restart": "unless-stopped",
                "deploy": {
                    "replicas": defaults["replicas"],
                    "resources": {"limits": {"cpus": str(cpu_cores), "memory": defaults["memory"]}},
                },
                "healthcheck": {
                    "test": ["CMD", "python", "-c", f"import urllib.request; urllib.request.urlopen('http://localhost:{port}/health')"],
                    "interval": "30s", "timeout": "5s", "retries": 3,
                },
            }
        },
    }
    if extra_services:
        compose["services"].update(extra_services)
    return yaml.safe_dump(compose, sort_keys=False, default_flow_style=False)
