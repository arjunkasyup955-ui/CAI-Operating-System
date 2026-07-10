from typing import Literal

from pydantic import BaseModel, Field

from core.registries import ToolSpec, get_tool_registry
from core.registries.tool_registry import RetryPolicy
from tools.build.build_providers import build_node_args, build_python_args, get_build_provider

_RETRY_POLICY = RetryPolicy(max_attempts=2, backoff_seconds=1.0)
_TOOL_TIMEOUT_SECONDS = 150.0


class PythonBuildArgs(BaseModel):
    action: Literal["install", "test", "build"]
    extra_args: list[str] = []


class NodeBuildArgs(BaseModel):
    action: Literal["install", "test", "build"]
    extra_args: list[str] = []


class GenericBuildArgs(BaseModel):
    command: list[str] = Field(min_length=1)


def build_python(action: str, extra_args: list[str] | None = None) -> dict:
    args = build_python_args(action, extra_args)
    return get_build_provider().run("python", args, timeout_seconds=120.0).model_dump()


def build_node(action: str, extra_args: list[str] | None = None) -> dict:
    args = build_node_args(action, extra_args)
    return get_build_provider().run("node", args, timeout_seconds=120.0).model_dump()


def build_generic(command: list[str]) -> dict:
    return get_build_provider().run("generic", command, timeout_seconds=120.0).model_dump()


_TOOLS: list[tuple[str, str, type[BaseModel], object]] = [
    ("build_python", "Run a Python build workflow action (install/test/build)", PythonBuildArgs, build_python),
    ("build_node", "Run a Node.js build workflow action (install/test/build)", NodeBuildArgs, build_node),
    ("build_generic", "Run a generic, project-agnostic build command", GenericBuildArgs, build_generic),
]

for _name, _description, _schema, _func in _TOOLS:
    get_tool_registry().register(
        ToolSpec(
            name=_name,
            description=_description,
            input_schema=_schema,
            permissions=["shell_exec"],
            retry_policy=_RETRY_POLICY,
            timeout_seconds=_TOOL_TIMEOUT_SECONDS,
            cost_per_call_usd=0.0,
        ),
        _func,
    )
