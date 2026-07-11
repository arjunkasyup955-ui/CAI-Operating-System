class Role:
    FOUNDER = "founder"
    ADMIN = "admin"
    REVIEWER = "reviewer"
    CONTRIBUTOR = "contributor"
    VIEWER = "viewer"


class Permission:
    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_CHANGES = "request_changes"
    COMMENT = "comment"
    ASSIGN_TASK = "assign_task"
    MANAGE_TEAM = "manage_team"
    EDIT_NOTES = "edit_notes"
    VIEW = "view"


# Pure data - the same "deterministic, pure-Python, no LLM" convention every
# prior policy table in AFOS uses (e.g. core.metrics.health's status
# thresholds). Roles are additive: each includes every permission of the
# roles listed before it, spelled out explicitly rather than inherited, so
# the table is the single source of truth with no hidden resolution order.
ROLE_PERMISSIONS: dict[str, set[str]] = {
    Role.VIEWER: {Permission.VIEW},
    Role.CONTRIBUTOR: {Permission.VIEW, Permission.COMMENT},
    Role.REVIEWER: {Permission.VIEW, Permission.COMMENT, Permission.APPROVE, Permission.REJECT, Permission.REQUEST_CHANGES},
    Role.ADMIN: {
        Permission.VIEW, Permission.COMMENT, Permission.APPROVE, Permission.REJECT, Permission.REQUEST_CHANGES,
        Permission.ASSIGN_TASK, Permission.MANAGE_TEAM, Permission.EDIT_NOTES,
    },
    Role.FOUNDER: {
        Permission.VIEW, Permission.COMMENT, Permission.APPROVE, Permission.REJECT, Permission.REQUEST_CHANGES,
        Permission.ASSIGN_TASK, Permission.MANAGE_TEAM, Permission.EDIT_NOTES,
    },
}


def role_has_permission(role: str, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, set())
