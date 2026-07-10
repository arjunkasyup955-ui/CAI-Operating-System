import subprocess
import time
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

# tools/terminal/terminal_providers.py -> tools/terminal -> tools -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]

# Command signatures that are never executed, regardless of approval - the terminal
# equivalent of Git's reset --hard / clean -fd / force-push ban.
_BANNED_EXECUTABLES = {"format", "mkfs", "diskpart", "shutdown", "reboot", "halt", "dd"}
_BANNED_SUBSTRINGS = ["rm -rf", "rm -fr", "del /f /s /q", ":(){ :|:& };:", "> /dev/sd", "mkfs."]

# Flags that hand an interpreter arbitrary inline code - a denylist on argv tokens
# cannot see what's *inside* a `-c`/`-e` payload, so anything using one of these is
# always treated as high risk regardless of the executable. This is a real, honest
# limitation: a sufficiently generic interpreter can always hide destructive behavior
# from static argv inspection - flagging inline-code flags raises the bar (forces
# human approval) but does not make arbitrary code execution provably safe.
_INLINE_CODE_FLAGS = {"-c", "-e", "--eval", "-command", "/c"}

_PACKAGE_MANAGERS = {"pip", "pip3", "npm", "npx", "yarn", "pnpm", "apt", "apt-get", "choco", "winget", "brew", "conda"}
_INSTALL_SUBCOMMANDS = {"install", "uninstall", "add", "remove", "update", "upgrade"}
_LOW_RISK_EXECUTABLES = {
    "python", "python3", "pytest", "node", "echo", "pwd", "ls", "dir", "cat", "type",
    "which", "where", "git",
}


class DestructiveCommandError(Exception):
    pass


def _executable_name(args: list[str]) -> str:
    return Path(args[0]).stem.lower()


def is_banned(args: list[str]) -> bool:
    if not args:
        return True
    if _executable_name(args) in _BANNED_EXECUTABLES:
        return True
    joined = " ".join(args).lower()
    return any(pattern in joined for pattern in _BANNED_SUBSTRINGS)


def classify_risk(args: list[str]) -> str:
    """low -> auto-approved under the default risk_based policy; high -> pauses for
    human approval. Unknown executables default to high (deny-by-default), matching
    the Permission Layer's own posture.
    """
    if not args:
        return "high"
    if any(a.lower() in _INLINE_CODE_FLAGS for a in args[1:]):
        return "high"

    exe = _executable_name(args)
    if exe in _PACKAGE_MANAGERS:
        if len(args) > 1 and args[1].lower() in _INSTALL_SUBCOMMANDS:
            return "high"
        return "medium"
    if exe in _LOW_RISK_EXECUTABLES:
        return "low"
    return "high"


class TerminalExecutionResult(BaseModel):
    success: bool
    command: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int = -1
    duration_seconds: float = 0.0


class TerminalProvider(Protocol):
    name: str

    def execute(self, args: list[str], timeout_seconds: float = 30.0) -> TerminalExecutionResult: ...


class SubprocessTerminalProvider:
    """Default TerminalProvider. Never uses shell=True and never accepts a shell
    string - args is always a list of literal argv tokens, so there is no shell
    metacharacter (;, &&, |, `` ` ``, $()) for the OS to interpret in the first place.
    Confined to `root` (the repo root by default) as the working directory.
    """

    name = "subprocess"

    def __init__(self, root: str | None = None) -> None:
        self._root = Path(root).resolve() if root else _REPO_ROOT

    def execute(self, args: list[str], timeout_seconds: float = 30.0) -> TerminalExecutionResult:
        if not args:
            raise ValueError("command must not be empty")
        if is_banned(args):
            raise DestructiveCommandError(f"refused to run a banned/destructive command: {' '.join(args)}")

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
        return TerminalExecutionResult(
            success=result.returncode == 0,
            command=" ".join(args),
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.returncode,
            duration_seconds=duration,
        )


_provider: TerminalProvider = SubprocessTerminalProvider()


def get_terminal_provider() -> TerminalProvider:
    return _provider


def set_terminal_provider(provider: TerminalProvider) -> None:
    """Swappable, not cached - lets tests inject a fake provider (or a real one rooted
    at an isolated temp directory) without depending on the actual repository state.
    """
    global _provider
    _provider = provider
