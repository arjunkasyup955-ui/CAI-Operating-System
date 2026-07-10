import os
from typing import Any, Protocol

from dotenv import load_dotenv
from pydantic import BaseModel

# Deliberately self-contained (own env reading, not core.config.Settings) so this tool
# module never needs the frozen Phase 0 kernel to change to gain a new config key -
# same convention as tools/web, tools/browser, tools/ollama, tools/database, tools/chroma.
load_dotenv()


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


class DockerHealthStatus(BaseModel):
    healthy: bool
    version: str = ""
    error: str = ""


class DockerContainerInfo(BaseModel):
    id: str
    name: str = ""
    image: str = ""
    status: str = ""
    state: str = ""


class DockerImageInfo(BaseModel):
    id: str
    repo_tags: list[str] = []
    size_bytes: int = 0


class DockerOperationResult(BaseModel):
    success: bool
    operation: str
    output: str = ""
    error: str = ""
    containers: list[DockerContainerInfo] = []
    images: list[DockerImageInfo] = []
    container_id: str = ""


class DockerProvider(Protocol):
    """Container/image lifecycle management. Every method either returns a *Result on
    success or raises on failure - the agent layer's try/except (with ToolRegistry's
    RetryPolicy) handles graceful degradation uniformly, the same pattern as every
    prior provider.
    """

    name: str

    def health_check(self) -> DockerHealthStatus: ...
    def list_containers(self, show_all: bool = True) -> DockerOperationResult: ...
    def list_images(self) -> DockerOperationResult: ...
    def pull_image(self, image_ref: str) -> DockerOperationResult: ...
    def create_container(self, image_ref: str, container_name: str | None = None, command: list[str] | None = None) -> DockerOperationResult: ...
    def start_container(self, container_id: str) -> DockerOperationResult: ...
    def stop_container(self, container_id: str, force: bool = False, timeout_seconds: int = 10) -> DockerOperationResult: ...
    def restart_container(self, container_id: str) -> DockerOperationResult: ...
    def remove_container(self, container_id: str, force: bool = False) -> DockerOperationResult: ...
    def container_logs(self, container_id: str, tail: int = 100) -> DockerOperationResult: ...
    def exec_command(self, container_id: str, command: list[str]) -> DockerOperationResult: ...


class DockerSDKProvider:
    """Default DockerProvider - real container management via the `docker` SDK
    (docker-py). Lazily imported so this module loads fine even when it isn't
    installed. In this sandbox, a stub/unrelated `docker` module is importable but
    lacks `from_env` (raises AttributeError rather than ImportError) - both are caught
    and reported the same way, exactly the same defensive pattern as
    PlaywrightProvider/Crawl4AIProvider and PsycopgPostgresProvider.
    """

    name = "docker_sdk"

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = base_url or _env("DOCKER_HOST", "")
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import docker

                self._client = docker.from_env() if not self._base_url else docker.DockerClient(base_url=self._base_url)
            except (ImportError, AttributeError) as exc:
                raise RuntimeError(
                    "the docker SDK (docker-py) is not installed or not functional - `pip install docker`"
                ) from exc
        return self._client

    def health_check(self) -> DockerHealthStatus:
        try:
            client = self._get_client()
            version = client.version()
            return DockerHealthStatus(healthy=True, version=version.get("Version", ""))
        except Exception as exc:
            return DockerHealthStatus(healthy=False, error=str(exc))

    def list_containers(self, show_all: bool = True) -> DockerOperationResult:
        client = self._get_client()
        containers = client.containers.list(all=show_all)
        infos = [
            DockerContainerInfo(id=c.id, name=c.name, image=str(c.image.tags or c.image.id), status=c.status, state=c.status)
            for c in containers
        ]
        return DockerOperationResult(success=True, operation="list_containers", containers=infos)

    def list_images(self) -> DockerOperationResult:
        client = self._get_client()
        images = client.images.list()
        infos = [DockerImageInfo(id=i.id, repo_tags=i.tags, size_bytes=i.attrs.get("Size", 0)) for i in images]
        return DockerOperationResult(success=True, operation="list_images", images=infos)

    def pull_image(self, image_ref: str) -> DockerOperationResult:
        client = self._get_client()
        client.images.pull(image_ref)
        return DockerOperationResult(success=True, operation="pull_image", output=f"pulled {image_ref}")

    def create_container(self, image_ref: str, container_name: str | None = None, command: list[str] | None = None) -> DockerOperationResult:
        client = self._get_client()
        container = client.containers.create(image_ref, name=container_name, command=command)
        return DockerOperationResult(success=True, operation="create_container", container_id=container.id)

    def start_container(self, container_id: str) -> DockerOperationResult:
        client = self._get_client()
        container = client.containers.get(container_id)
        container.start()
        return DockerOperationResult(success=True, operation="start_container", container_id=container_id)

    def stop_container(self, container_id: str, force: bool = False, timeout_seconds: int = 10) -> DockerOperationResult:
        client = self._get_client()
        container = client.containers.get(container_id)
        if force:
            container.kill()
        else:
            container.stop(timeout=timeout_seconds)
        return DockerOperationResult(success=True, operation="stop_container", container_id=container_id)

    def restart_container(self, container_id: str) -> DockerOperationResult:
        client = self._get_client()
        container = client.containers.get(container_id)
        container.restart()
        return DockerOperationResult(success=True, operation="restart_container", container_id=container_id)

    def remove_container(self, container_id: str, force: bool = False) -> DockerOperationResult:
        client = self._get_client()
        container = client.containers.get(container_id)
        container.remove(force=force)
        return DockerOperationResult(success=True, operation="remove_container", container_id=container_id)

    def container_logs(self, container_id: str, tail: int = 100) -> DockerOperationResult:
        client = self._get_client()
        container = client.containers.get(container_id)
        logs = container.logs(tail=tail).decode("utf-8", errors="replace")
        return DockerOperationResult(success=True, operation="container_logs", output=logs, container_id=container_id)

    def exec_command(self, container_id: str, command: list[str]) -> DockerOperationResult:
        client = self._get_client()
        container = client.containers.get(container_id)
        exit_code, output = container.exec_run(command)
        return DockerOperationResult(
            success=(exit_code == 0),
            operation="exec_command",
            output=output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output),
            container_id=container_id,
        )


class FakeDockerProvider:
    """In-memory DockerProvider - deterministic, no real Docker daemon needed.
    Simulates a genuine container lifecycle (create -> start -> logs -> stop ->
    restart -> remove) with real state transitions, so lifecycle tests exercise real
    logic rather than canned responses.
    """

    name = "fake_docker"

    def __init__(self) -> None:
        self._containers: dict[str, dict[str, Any]] = {}
        self._images: dict[str, dict[str, Any]] = {}
        self._counter = 0

    def health_check(self) -> DockerHealthStatus:
        return DockerHealthStatus(healthy=True, version="fake-docker-1.0")

    def list_containers(self, show_all: bool = True) -> DockerOperationResult:
        containers = list(self._containers.values())
        if not show_all:
            containers = [c for c in containers if c["state"] == "running"]
        infos = [DockerContainerInfo(id=c["id"], name=c["name"], image=c["image"], status=c["state"], state=c["state"]) for c in containers]
        return DockerOperationResult(success=True, operation="list_containers", containers=infos)

    def list_images(self) -> DockerOperationResult:
        infos = [DockerImageInfo(id=i["id"], repo_tags=[ref], size_bytes=i["size_bytes"]) for ref, i in self._images.items()]
        return DockerOperationResult(success=True, operation="list_images", images=infos)

    def pull_image(self, image_ref: str) -> DockerOperationResult:
        self._images[image_ref] = {"id": f"sha256:fake-{image_ref}", "size_bytes": 13000}
        return DockerOperationResult(success=True, operation="pull_image", output=f"pulled {image_ref}")

    def create_container(self, image_ref: str, container_name: str | None = None, command: list[str] | None = None) -> DockerOperationResult:
        if image_ref not in self._images:
            raise ValueError(f"image '{image_ref}' has not been pulled")
        self._counter += 1
        container_id = f"fake-container-{self._counter}"
        self._containers[container_id] = {
            "id": container_id,
            "name": container_name or container_id,
            "image": image_ref,
            "command": command or [],
            "state": "created",
            "logs": "",
        }
        return DockerOperationResult(success=True, operation="create_container", container_id=container_id)

    def _get(self, container_id: str) -> dict[str, Any]:
        if container_id not in self._containers:
            raise ValueError(f"no such container: {container_id}")
        return self._containers[container_id]

    def start_container(self, container_id: str) -> DockerOperationResult:
        container = self._get(container_id)
        container["state"] = "running"
        container["logs"] += f"Hello from {container['image']}\n"
        return DockerOperationResult(success=True, operation="start_container", container_id=container_id)

    def stop_container(self, container_id: str, force: bool = False, timeout_seconds: int = 10) -> DockerOperationResult:
        container = self._get(container_id)
        container["state"] = "exited"
        return DockerOperationResult(success=True, operation="stop_container", container_id=container_id)

    def restart_container(self, container_id: str) -> DockerOperationResult:
        container = self._get(container_id)
        container["state"] = "running"
        return DockerOperationResult(success=True, operation="restart_container", container_id=container_id)

    def remove_container(self, container_id: str, force: bool = False) -> DockerOperationResult:
        container = self._get(container_id)
        if container["state"] == "running" and not force:
            raise RuntimeError(f"container '{container_id}' is running - stop it or use force=True")
        del self._containers[container_id]
        return DockerOperationResult(success=True, operation="remove_container", container_id=container_id)

    def container_logs(self, container_id: str, tail: int = 100) -> DockerOperationResult:
        container = self._get(container_id)
        lines = container["logs"].splitlines()[-tail:]
        return DockerOperationResult(success=True, operation="container_logs", output="\n".join(lines), container_id=container_id)

    def exec_command(self, container_id: str, command: list[str]) -> DockerOperationResult:
        container = self._get(container_id)
        if container["state"] != "running":
            raise RuntimeError(f"container '{container_id}' is not running")
        output = f"executed {' '.join(command)} in {container_id}"
        return DockerOperationResult(success=True, operation="exec_command", output=output, container_id=container_id)


_provider: DockerProvider = DockerSDKProvider()


def get_docker_provider() -> DockerProvider:
    return _provider


def set_docker_provider(provider: DockerProvider) -> None:
    """Swappable, not cached - lets tests inject a deterministic fake provider."""
    global _provider
    _provider = provider
