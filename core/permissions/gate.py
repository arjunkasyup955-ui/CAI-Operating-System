from enum import StrEnum
from functools import lru_cache

from core.registries.agent_registry import AgentManifest


class Permission(StrEnum):
    FILE_ACCESS = "file_access"
    INTERNET_ACCESS = "internet_access"
    SHELL_EXEC = "shell_exec"
    GIT_OPS = "git_ops"
    DEPLOYMENT = "deployment"
    PAYMENTS = "payments"
    ADVERTISEMENT = "advertisement"
    CRM = "crm"


class PermissionDeniedError(Exception):
    pass


class PermissionGate:
    """Deny-by-default. An agent can only do what its manifest explicitly grants -
    no tool invocation should bypass this check.
    """

    def check(self, manifest: AgentManifest, action: Permission | str) -> None:
        action_value = action.value if isinstance(action, Permission) else action
        if action_value not in manifest.permissions:
            raise PermissionDeniedError(
                f"agent '{manifest.name}' is not granted permission '{action_value}'"
            )

    def is_allowed(self, manifest: AgentManifest, action: Permission | str) -> bool:
        try:
            self.check(manifest, action)
            return True
        except PermissionDeniedError:
            return False


@lru_cache
def get_permission_gate() -> PermissionGate:
    return PermissionGate()
