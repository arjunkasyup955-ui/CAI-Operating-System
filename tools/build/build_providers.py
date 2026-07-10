import subprocess
import sys
import time
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from tools.terminal.terminal_providers import DestructiveCommandError, is_banned

# tools/build/build_providers.py -> tools/build -> tools -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]

# Base argv per build_type/action - the tool layer and the agent layer (for
# pre-approval risk classification) both import these, so the two never drift apart.
PYTHON_BASE_COMMANDS: dict[str, list[str]] = {
    "install": [sys.executable, "-m", "pip", "install", "-e", "."],
    "test": [sys.executable, "-m", "pytest"],
    "build": [sys.executable, "-m", "build"],
}
NODE_BASE_COMMANDS: dict[str, list[str]] = {
    "install": ["npm", "install"],
    "test": ["npm", "test"],
    "build": ["npm", "run", "build"],
}

_HIGH_RISK_KEYWORDS = {"publish", "deploy", "upload", "release"}


def build_python_args(action: str, extra_args: list[str] | None = None) -> list[str]:
    if action not in PYTHON_BASE_COMMANDS:
        raise ValueError(f"unknown python build action: {action}")
    return [*PYTHON_BASE_COMMANDS[action], *(extra_args or [])]


def build_node_args(action: str, extra_args: list[str] | None = None) -> list[str]:
    if action not in NODE_BASE_COMMANDS:
        raise ValueError(f"unknown node build action: {action}")
    return [*NODE_BASE_COMMANDS[action], *(extra_args or [])]


def classify_build_risk(action: str, args: list[str]) -> str:
    """low -> auto-approved under the default risk_based policy; high -> pauses for
    human approval. Dependency installation, publishing, deployment, package upload,
    and system modification are always high risk; unrecognized actions default to
    high too (deny-by-default), matching the Terminal Agent's posture.
    """
    combined = " ".join(args).lower()
    if any(keyword in combined for keyword in _HIGH_RISK_KEYWORDS):
        return "high"
    if action == "install":
        return "high"
    if action == "test":
        return "low"
    if action == "build":
        return "medium"
    return "high"


class BuildResult(BaseModel):
    success: bool
    build_type: str
    command: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int = -1
    duration_seconds: float = 0.0
    status: str = ""


class BuildProvider(Protocol):
    name: str

    def run(self, build_type: str, args: list[str], timeout_seconds: float = 120.0) -> BuildResult: ...


class SubprocessBuildProvider:
    """Default BuildProvider. Never uses shell=True - args is always a list of literal
    argv tokens. Reuses the Terminal Agent's destructive-command denylist (imported,
    not copy-pasted) so the two banned-pattern lists can never drift out of sync.
    Confined to `root` (the repo root by default) as the working directory.
    """

    name = "subprocess_build"

    def __init__(self, root: str | None = None) -> None:
        self._root = Path(root).resolve() if root else _REPO_ROOT

    def run(self, build_type: str, args: list[str], timeout_seconds: float = 120.0) -> BuildResult:
        if not args:
            raise ValueError("build command must not be empty")
        if is_banned(args):
            raise DestructiveCommandError(f"refused to run a banned/destructive build command: {' '.join(args)}")

        start = time.monotonic()
        result = subprocess.run(
            args,
            cwd=str(self._root),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            shell=False,
        )
        duration = time.monotonic() - start
        return BuildResult(
            success=result.returncode == 0,
            build_type=build_type,
            command=" ".join(args),
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.returncode,
            duration_seconds=duration,
            status="passed" if result.returncode == 0 else "failed",
        )


_provider: BuildProvider = SubprocessBuildProvider()


def get_build_provider() -> BuildProvider:
    return _provider


def set_build_provider(provider: BuildProvider) -> None:
    """Swappable, not cached - lets tests inject a fake provider (or a real one rooted
    at an isolated temp directory) without depending on the actual repository state.
    """
    global _provider
    _provider = provider
