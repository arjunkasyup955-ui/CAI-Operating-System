import subprocess
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

# Any git args containing one of these combinations are refused before subprocess ever
# runs - defense in depth on top of the tool surface only ever building safe args
# (status/add/commit/checkout/branch/diff/log, never raw/arbitrary flags).
_DESTRUCTIVE_COMBINATIONS: list[set[str]] = [
    {"reset", "--hard"},
    {"clean", "-fd"},
    {"clean", "-f"},
    {"clean", "-d"},
    {"push", "--force"},
    {"push", "-f"},
]


class DestructiveGitOperationError(Exception):
    pass


def is_destructive(args: list[str]) -> bool:
    arg_set = set(args)
    return any(combo.issubset(arg_set) for combo in _DESTRUCTIVE_COMBINATIONS)


def reject_flag_like(value: str, field_name: str) -> None:
    """Prevents flag-smuggling: a user-controlled value like '--force' passed as a
    path/ref/branch-name could otherwise be interpreted by git as a flag rather than
    a literal argument.
    """
    if value.startswith("-"):
        raise ValueError(f"{field_name} must not look like a flag: {value!r}")


class GitOperationResult(BaseModel):
    success: bool
    operation: str
    output: str = ""
    error: str = ""
    exit_code: int = 0


class GitProvider(Protocol):
    name: str

    def run(self, args: list[str]) -> GitOperationResult: ...


class SubprocessGitProvider:
    """Default GitProvider - runs the real `git` CLI via subprocess against this
    repository. Refuses any destructive argument combination before ever invoking
    subprocess, regardless of what called it.
    """

    name = "git_cli"

    def __init__(self, cwd: str | None = None) -> None:
        # tools/git/git_providers.py -> tools/git -> tools -> repo root
        self._cwd = cwd or str(Path(__file__).resolve().parents[2])

    def run(self, args: list[str]) -> GitOperationResult:
        if is_destructive(args):
            raise DestructiveGitOperationError(f"refused to run destructive git command: git {' '.join(args)}")

        result = subprocess.run(
            ["git", *args],
            cwd=self._cwd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return GitOperationResult(
            success=result.returncode == 0,
            operation=" ".join(args),
            output=result.stdout,
            error=result.stderr,
            exit_code=result.returncode,
        )


_provider: GitProvider = SubprocessGitProvider()


def get_git_provider() -> GitProvider:
    return _provider


def set_git_provider(provider: GitProvider) -> None:
    """Swappable, not cached - lets tests inject a fake provider without touching the
    real repository, then restore the real one afterward.
    """
    global _provider
    _provider = provider
