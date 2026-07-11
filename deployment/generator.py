import json
from pathlib import Path
from typing import Any

import yaml

from deployment.docker.generator import generate_docker_compose, generate_dockerfile
from deployment.environment import generate_env_example
from deployment.flyio.generator import generate_flyio_config
from deployment.github.generator import generate_github_actions_workflow
from deployment.kubernetes.generator import (
    generate_k8s_deployment,
    generate_k8s_ingress,
    generate_k8s_secrets_template,
    generate_k8s_service,
)
from deployment.railway.generator import generate_railway_config
from deployment.render.generator import generate_render_config
from deployment.targets import ALL_TARGETS, DeploymentProfile, DeploymentTarget
from deployment.vercel.generator import generate_vercel_config


class DeploymentGenerator:
    """Top-level orchestrator dispatching to each target's own generator
    module - never reimplements a single target's logic, only combines their
    outputs into a {relative_file_path: content} artifact map and (only when
    explicitly asked, via write_to_disk) writes them into a NEW,
    caller-specified output directory. Never touches any existing tracked
    source file.
    """

    def generate(
        self,
        project_name: str,
        target: str,
        profile: str = DeploymentProfile.PRODUCTION,
        port: int = 8000,
        health_path: str = "/health",
        entrypoint: str = "apps/api/main.py",
        image: str | None = None,
        host: str | None = None,
        env_vars: list[str] | None = None,
        secret_keys: list[str] | None = None,
        deploy_target_for_ci: str | None = None,
    ) -> dict[str, str]:
        env_vars = env_vars or []
        secret_keys = secret_keys or []
        image = image or f"{project_name}:latest"
        host = host or f"{project_name}.example.com"

        if target == DeploymentTarget.DOCKER:
            return {"Dockerfile": generate_dockerfile(project_name, entrypoint=entrypoint, port=port, health_path=health_path, profile=profile)}

        if target == DeploymentTarget.DOCKER_COMPOSE:
            return {"docker-compose.yml": generate_docker_compose(project_name, port=port, profile=profile, env_vars=env_vars)}

        if target == DeploymentTarget.GITHUB:
            return {".github/workflows/ci-cd.yml": generate_github_actions_workflow(project_name, deploy_target=deploy_target_for_ci)}

        if target == DeploymentTarget.VERCEL:
            return {"vercel.json": json.dumps(generate_vercel_config(project_name, env_vars=env_vars), indent=2) + "\n"}

        if target == DeploymentTarget.RAILWAY:
            start_command = f"python {entrypoint}"
            return {"railway.json": json.dumps(generate_railway_config(project_name, start_command, healthcheck_path=health_path, profile=profile), indent=2) + "\n"}

        if target == DeploymentTarget.RENDER:
            start_command = f"python {entrypoint}"
            return {"render.yaml": yaml.safe_dump(generate_render_config(project_name, start_command, health_check_path=health_path, profile=profile, env_vars=env_vars), sort_keys=False, default_flow_style=False)}

        if target == DeploymentTarget.FLYIO:
            return {"fly.toml": generate_flyio_config(project_name, internal_port=port, profile=profile, env_vars=env_vars)}

        if target == DeploymentTarget.KUBERNETES:
            deployment = generate_k8s_deployment(project_name, image=image, port=port, health_path=health_path, profile=profile, env_vars=env_vars)
            service = generate_k8s_service(project_name, target_port=port)
            ingress = generate_k8s_ingress(project_name, host=host)
            secrets = generate_k8s_secrets_template(project_name, secret_keys or env_vars)
            return {
                "k8s/deployment.yaml": yaml.safe_dump(deployment, sort_keys=False, default_flow_style=False),
                "k8s/service.yaml": yaml.safe_dump(service, sort_keys=False, default_flow_style=False),
                "k8s/ingress.yaml": yaml.safe_dump(ingress, sort_keys=False, default_flow_style=False),
                "k8s/secrets.yaml": yaml.safe_dump(secrets, sort_keys=False, default_flow_style=False),
            }

        raise ValueError(f"unknown deployment target: {target!r} (expected one of {ALL_TARGETS})")

    def generate_all(self, project_name: str, profile: str = DeploymentProfile.PRODUCTION, **kwargs: Any) -> dict[str, dict[str, str]]:
        return {target: self.generate(project_name, target, profile=profile, **kwargs) for target in ALL_TARGETS}

    def generate_environment_artifacts(self, env_vars: list[str], secret_keys: list[str] | None = None) -> dict[str, str]:
        return {".env.example": generate_env_example(env_vars, secret_keys=secret_keys)}

    def write_to_disk(self, artifacts: dict[str, str], output_dir: str | Path) -> list[Path]:
        output_dir = Path(output_dir)
        written: list[Path] = []
        for relative_path, content in artifacts.items():
            file_path = output_dir / relative_path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
            written.append(file_path)
        return written
