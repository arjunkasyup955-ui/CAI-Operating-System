class DeploymentTarget:
    GITHUB = "github"
    DOCKER = "docker"
    DOCKER_COMPOSE = "docker_compose"
    VERCEL = "vercel"
    RAILWAY = "railway"
    RENDER = "render"
    FLYIO = "flyio"
    KUBERNETES = "kubernetes"


ALL_TARGETS = (
    DeploymentTarget.GITHUB, DeploymentTarget.DOCKER, DeploymentTarget.DOCKER_COMPOSE,
    DeploymentTarget.VERCEL, DeploymentTarget.RAILWAY, DeploymentTarget.RENDER,
    DeploymentTarget.FLYIO, DeploymentTarget.KUBERNETES,
)


class DeploymentProfile:
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
    ENTERPRISE = "enterprise"


ALL_PROFILES = (
    DeploymentProfile.DEVELOPMENT, DeploymentProfile.STAGING,
    DeploymentProfile.PRODUCTION, DeploymentProfile.ENTERPRISE,
)

# Pure data - deterministic, no LLM, the same convention every prior policy/
# health/retry table in AFOS uses. Each profile's resource sizing scales up
# from development (single, unmonitored instance) through enterprise (many
# replicas, generous headroom).
_PROFILE_DEFAULTS: dict[str, dict[str, object]] = {
    DeploymentProfile.DEVELOPMENT: {"replicas": 1, "cpu": "250m", "memory": "256Mi", "min_instances": 0, "max_instances": 1, "log_level": "DEBUG"},
    DeploymentProfile.STAGING: {"replicas": 1, "cpu": "500m", "memory": "512Mi", "min_instances": 1, "max_instances": 2, "log_level": "INFO"},
    DeploymentProfile.PRODUCTION: {"replicas": 2, "cpu": "1000m", "memory": "1Gi", "min_instances": 2, "max_instances": 5, "log_level": "WARNING"},
    DeploymentProfile.ENTERPRISE: {"replicas": 4, "cpu": "2000m", "memory": "2Gi", "min_instances": 4, "max_instances": 20, "log_level": "WARNING"},
}

_RENDER_PLAN_BY_PROFILE: dict[str, str] = {
    DeploymentProfile.DEVELOPMENT: "free",
    DeploymentProfile.STAGING: "starter",
    DeploymentProfile.PRODUCTION: "standard",
    DeploymentProfile.ENTERPRISE: "pro",
}


def resolve_profile_defaults(profile: str) -> dict[str, object]:
    return dict(_PROFILE_DEFAULTS.get(profile, _PROFILE_DEFAULTS[DeploymentProfile.PRODUCTION]))


def resolve_render_plan(profile: str) -> str:
    return _RENDER_PLAN_BY_PROFILE.get(profile, "starter")
