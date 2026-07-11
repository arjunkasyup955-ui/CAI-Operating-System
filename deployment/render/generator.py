from deployment.targets import DeploymentProfile, resolve_profile_defaults, resolve_render_plan


def generate_render_config(
    project_name: str,
    start_command: str,
    health_check_path: str = "/health",
    profile: str = DeploymentProfile.PRODUCTION,
    env_vars: list[str] | None = None,
) -> dict:
    """Returns the render.yaml content as a dict - serialized to YAML text by
    the top-level DeploymentGenerator.
    """
    defaults = resolve_profile_defaults(profile)
    return {
        "services": [
            {
                "type": "web",
                "name": project_name,
                "env": "docker",
                "plan": resolve_render_plan(profile),
                "startCommand": start_command,
                "healthCheckPath": health_check_path,
                "numInstances": defaults["replicas"],
                "envVars": [{"key": key, "sync": False} for key in (env_vars or [])],
            }
        ]
    }
