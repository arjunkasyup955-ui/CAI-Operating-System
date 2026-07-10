from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel


class AgentManifest(BaseModel):
    """What every agent declares about itself. The Manager/Supervisors route by querying
    this registry instead of hardcoding imports - required once there are 100+ agents.
    """

    name: str
    responsibilities: list[str]
    inputs: list[str] = []
    outputs: list[str] = []
    tools: list[str] = []
    permissions: list[str] = []
    supervisor: str | None = None
    lifecycle: Literal["draft", "active", "deprecated"] = "draft"
    version: str = "0.1.0"


def load_manifest(path: str | Path) -> AgentManifest:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return AgentManifest(**data)


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, AgentManifest] = {}

    def register(self, manifest: AgentManifest) -> None:
        self._agents[manifest.name] = manifest

    def register_from_yaml(self, path: str | Path) -> AgentManifest:
        manifest = load_manifest(path)
        self.register(manifest)
        return manifest

    def get(self, name: str) -> AgentManifest:
        if name not in self._agents:
            raise KeyError(f"agent '{name}' is not registered")
        return self._agents[name]

    def list_all(self) -> list[AgentManifest]:
        return list(self._agents.values())

    def list_by_supervisor(self, supervisor: str) -> list[AgentManifest]:
        return [a for a in self._agents.values() if a.supervisor == supervisor]


@lru_cache
def get_agent_registry() -> AgentRegistry:
    return AgentRegistry()
