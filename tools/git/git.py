from pydantic import BaseModel

from core.registries import ToolSpec, get_tool_registry
from core.registries.tool_registry import RetryPolicy
from tools.git.git_providers import get_git_provider, reject_flag_like

_RETRY_POLICY = RetryPolicy(max_attempts=2, backoff_seconds=0.5)
_TIMEOUT_SECONDS = 30.0


class GitStatusArgs(BaseModel):
    pass


class GitAddArgs(BaseModel):
    paths: list[str]


class GitCommitArgs(BaseModel):
    message: str


class GitCheckoutArgs(BaseModel):
    ref: str
    create_new: bool = False


class GitBranchArgs(BaseModel):
    name: str | None = None


class GitDiffArgs(BaseModel):
    pass


class GitLogArgs(BaseModel):
    max_count: int = 10


def git_status() -> dict:
    return get_git_provider().run(["status"]).model_dump()


def git_add(paths: list[str]) -> dict:
    if not paths:
        raise ValueError("git_add requires at least one path")
    for path in paths:
        reject_flag_like(path, "path")
    return get_git_provider().run(["add", *paths]).model_dump()


def git_commit(message: str) -> dict:
    if not message.strip():
        raise ValueError("git_commit requires a non-empty message")
    reject_flag_like(message, "message")
    return get_git_provider().run(["commit", "-m", message]).model_dump()


def git_checkout(ref: str, create_new: bool = False) -> dict:
    reject_flag_like(ref, "ref")
    args = ["checkout", "-b", ref] if create_new else ["checkout", ref]
    return get_git_provider().run(args).model_dump()


def git_branch(name: str | None = None) -> dict:
    if name is None:
        return get_git_provider().run(["branch"]).model_dump()
    reject_flag_like(name, "name")
    return get_git_provider().run(["branch", name]).model_dump()


def git_diff() -> dict:
    return get_git_provider().run(["diff"]).model_dump()


def git_log(max_count: int = 10) -> dict:
    if max_count <= 0:
        raise ValueError("max_count must be positive")
    return get_git_provider().run(["log", f"-{max_count}", "--oneline"]).model_dump()


_TOOLS: list[tuple[str, str, type[BaseModel], object]] = [
    ("git_status", "Show the working tree status", GitStatusArgs, git_status),
    ("git_add", "Stage the given paths", GitAddArgs, git_add),
    ("git_commit", "Commit staged changes with a message", GitCommitArgs, git_commit),
    ("git_checkout", "Switch branches or restore working tree files", GitCheckoutArgs, git_checkout),
    ("git_branch", "List branches, or create one if a name is given", GitBranchArgs, git_branch),
    ("git_diff", "Show changes between commits, commit and working tree, etc.", GitDiffArgs, git_diff),
    ("git_log", "Show commit logs", GitLogArgs, git_log),
]

for _name, _description, _schema, _func in _TOOLS:
    get_tool_registry().register(
        ToolSpec(
            name=_name,
            description=_description,
            input_schema=_schema,
            permissions=["git_ops"],
            retry_policy=_RETRY_POLICY,
            timeout_seconds=_TIMEOUT_SECONDS,
            cost_per_call_usd=0.0,
        ),
        _func,
    )
