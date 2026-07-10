from pydantic import BaseModel, Field

from core.registries import ToolSpec, get_tool_registry
from core.registries.tool_registry import RetryPolicy
from tools.docker.docker_providers import get_docker_provider

_RETRY_POLICY = RetryPolicy(max_attempts=2, backoff_seconds=1.0)
_TIMEOUT_SECONDS = 30.0

# Field names are deliberately never "name" anywhere below - ToolRegistry.invoke()'s
# own positional parameter is literally called `name` (the tool's name), so a field
# called `name` passed through **kwargs collides with it. Found and fixed in the
# Chroma component (Phase 3, Component 3); avoided proactively here.


class HealthCheckArgs(BaseModel):
    pass


class ListContainersArgs(BaseModel):
    show_all: bool = True


class ListImagesArgs(BaseModel):
    pass


class PullImageArgs(BaseModel):
    image_ref: str


class CreateContainerArgs(BaseModel):
    image_ref: str
    container_name: str | None = None
    command: list[str] | None = None


class StartContainerArgs(BaseModel):
    container_id: str


class StopContainerArgs(BaseModel):
    container_id: str
    force: bool = False
    timeout_seconds: int = 10


class RestartContainerArgs(BaseModel):
    container_id: str


class RemoveContainerArgs(BaseModel):
    container_id: str
    force: bool = False


class ContainerLogsArgs(BaseModel):
    container_id: str
    tail: int = 100


class ExecCommandArgs(BaseModel):
    container_id: str
    command: list[str] = Field(min_length=1)


def docker_health_check() -> dict:
    return get_docker_provider().health_check().model_dump()


def docker_list_containers(show_all: bool = True) -> dict:
    return get_docker_provider().list_containers(show_all).model_dump()


def docker_list_images() -> dict:
    return get_docker_provider().list_images().model_dump()


def docker_pull_image(image_ref: str) -> dict:
    return get_docker_provider().pull_image(image_ref).model_dump()


def docker_create_container(image_ref: str, container_name: str | None = None, command: list[str] | None = None) -> dict:
    return get_docker_provider().create_container(image_ref, container_name, command).model_dump()


def docker_start_container(container_id: str) -> dict:
    return get_docker_provider().start_container(container_id).model_dump()


def docker_stop_container(container_id: str, force: bool = False, timeout_seconds: int = 10) -> dict:
    return get_docker_provider().stop_container(container_id, force, timeout_seconds).model_dump()


def docker_restart_container(container_id: str) -> dict:
    return get_docker_provider().restart_container(container_id).model_dump()


def docker_remove_container(container_id: str, force: bool = False) -> dict:
    return get_docker_provider().remove_container(container_id, force).model_dump()


def docker_container_logs(container_id: str, tail: int = 100) -> dict:
    return get_docker_provider().container_logs(container_id, tail).model_dump()


def docker_exec_command(container_id: str, command: list[str]) -> dict:
    return get_docker_provider().exec_command(container_id, command).model_dump()


_TOOLS: list[tuple[str, str, type[BaseModel], object, list[str]]] = [
    ("docker_health_check", "Check whether the Docker daemon is reachable", HealthCheckArgs, docker_health_check, ["deployment"]),
    ("docker_list_containers", "List containers", ListContainersArgs, docker_list_containers, ["deployment"]),
    ("docker_list_images", "List images", ListImagesArgs, docker_list_images, ["deployment"]),
    ("docker_pull_image", "Pull an image", PullImageArgs, docker_pull_image, ["deployment"]),
    ("docker_create_container", "Create a container from an image", CreateContainerArgs, docker_create_container, ["deployment"]),
    ("docker_start_container", "Start a container", StartContainerArgs, docker_start_container, ["deployment"]),
    ("docker_stop_container", "Stop a container", StopContainerArgs, docker_stop_container, ["deployment"]),
    ("docker_restart_container", "Restart a container", RestartContainerArgs, docker_restart_container, ["deployment"]),
    ("docker_remove_container", "Remove a container", RemoveContainerArgs, docker_remove_container, ["deployment"]),
    ("docker_container_logs", "Fetch a container's logs", ContainerLogsArgs, docker_container_logs, ["deployment"]),
    ("docker_exec_command", "Execute a command inside a running container", ExecCommandArgs, docker_exec_command, ["deployment", "shell_exec"]),
]

for _tool_name, _description, _schema, _func, _permissions in _TOOLS:
    get_tool_registry().register(
        ToolSpec(
            name=_tool_name,
            description=_description,
            input_schema=_schema,
            permissions=_permissions,
            retry_policy=_RETRY_POLICY,
            timeout_seconds=_TIMEOUT_SECONDS,
            cost_per_call_usd=0.0,
        ),
        _func,
    )
