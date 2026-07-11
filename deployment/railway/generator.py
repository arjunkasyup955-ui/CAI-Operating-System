from deployment.targets import DeploymentProfile, resolve_profile_defaults


def generate_railway_config(
    project_name: str,
    start_command: str,
    healthcheck_path: str = "/health",
    profile: str = DeploymentProfile.PRODUCTION,
) -> dict:
    defaults = resolve_profile_defaults(profile)
    return {
        "$schema": "https://railway.app/railway.schema.json",
        "build": {"builder": "DOCKERFILE"},
        "deploy": {
            "startCommand": start_command,
            "healthcheckPath": healthcheck_path,
            "healthcheckTimeout": 30,
            "restartPolicyType": "ON_FAILURE",
            "restartPolicyMaxRetries": 3,
            "numReplicas": defaults["replicas"],
        },
    }
