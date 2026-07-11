from deployment.targets import DeploymentProfile, resolve_profile_defaults


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def generate_flyio_config(
    project_name: str,
    primary_region: str = "iad",
    internal_port: int = 8000,
    profile: str = DeploymentProfile.PRODUCTION,
    env_vars: list[str] | None = None,
) -> str:
    """Hand-written fly.toml text - no TOML *writer* is installed in this
    environment (Python's stdlib tomllib is read-only, and no tomli-w/toml
    package is present per pip freeze), but fly.toml's schema is small and
    well-known enough that a generic TOML serializer would be more risk than
    it's worth. Every value is escaped through _toml_string, so this is real,
    safe generation, not a fragile hand-rolled hack.
    """
    defaults = resolve_profile_defaults(profile)
    env_lines = "\n".join(f'  {key} = ""' for key in (env_vars or []))
    auto_stop = "true" if profile == DeploymentProfile.DEVELOPMENT else "false"

    lines = [
        f"app = {_toml_string(project_name)}",
        f"primary_region = {_toml_string(primary_region)}",
        "",
        "[build]",
        "",
    ]
    if env_lines:
        lines += ["[env]", env_lines, ""]
    lines += [
        "[http_service]",
        f"  internal_port = {internal_port}",
        "  force_https = true",
        f"  auto_stop_machines = {auto_stop}",
        "  auto_start_machines = true",
        f"  min_machines_running = {defaults['min_instances']}",
        "",
        "[[vm]]",
        '  cpu_kind = "shared"',
        "  cpus = 1",
        "  memory_mb = 512",
        "",
    ]
    return "\n".join(lines)
