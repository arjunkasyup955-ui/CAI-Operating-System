from typing import Any

import yaml


def generate_github_actions_workflow(
    project_name: str,
    python_version: str = "3.11",
    test_command: str = "pytest",
    deploy_target: str | None = None,
    branch: str = "main",
) -> str:
    """Deterministic GitHub Actions CI/CD workflow. A deploy job is only
    included when deploy_target is given - "no duplicated code" means this
    never re-implements the actual deploy command for a target (that's each
    target's own generator's job); it only names the target in a placeholder
    step for the founder to fill in with their real deploy credentials/CLI.
    """
    workflow: dict[str, Any] = {
        "name": f"{project_name} CI/CD",
        "on": {"push": {"branches": [branch]}, "pull_request": {"branches": [branch]}},
        "jobs": {
            "test": {
                "runs-on": "ubuntu-latest",
                "steps": [
                    {"uses": "actions/checkout@v4"},
                    {"uses": "actions/setup-python@v5", "with": {"python-version": python_version}},
                    {"name": "Install dependencies", "run": "pip install -r requirements.txt"},
                    {"name": "Run tests", "run": test_command},
                ],
            }
        },
    }
    if deploy_target:
        workflow["jobs"]["deploy"] = {
            "needs": "test",
            "runs-on": "ubuntu-latest",
            "if": f"github.ref == 'refs/heads/{branch}'",
            "steps": [
                {"uses": "actions/checkout@v4"},
                {"name": f"Deploy to {deploy_target}", "run": f"echo 'configure real {deploy_target} deploy credentials/CLI here'"},
            ],
        }
    return yaml.safe_dump(workflow, sort_keys=False, default_flow_style=False)
