def generate_vercel_config(
    project_name: str,
    build_command: str = "npm run build",
    output_directory: str = "dist",
    install_command: str = "npm install",
    env_vars: list[str] | None = None,
) -> dict:
    """Returns the vercel.json content as a dict (JSON-serializable) - the
    top-level DeploymentGenerator is what turns this into actual file text
    via json.dumps, kept consistent with how every other target generator
    here returns structured/text content rather than writing files itself.
    """
    return {
        "name": project_name,
        "version": 2,
        "buildCommand": build_command,
        "installCommand": install_command,
        "outputDirectory": output_directory,
        "env": {key: f"@{key.lower()}" for key in (env_vars or [])},
    }
