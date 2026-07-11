import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from core.collaboration.activity import get_default_activity_store
from core.collaboration.permissions import Role
from core.event_bus import AFOSEvent, get_event_bus
from core.notifications import NotificationType, dispatch
from core.registries import get_agent_registry

get_agent_registry().register_from_yaml(Path(__file__).parent / "manifest.yaml")


def _publish(event_type: str, venture_id: str, approval_id: str, stage: str, extra: dict[str, Any] | None = None) -> None:
    """Publishes directly onto the Phase 0 Event Bus (unmodified) - the same
    mechanism every Phase 0-4 agent already uses to surface its own domain
    events. This alone is what makes every approval action automatically
    visible through Phase 5 Component 4's existing /api/dashboard/events
    endpoint (get_pipeline_events()), with zero Component 4 code touched.
    """
    get_event_bus().publish(
        AFOSEvent(
            type=f"human_approval_{event_type}", source_agent="human_approval", venture_id=venture_id,
            payload={"approval_id": approval_id, "stage": stage, **(extra or {})},
        )
    )


def _record_resolved_execution(request: dict[str, Any]) -> None:
    """Records every terminal approval decision into the same
    core.observability.ExecutionHistoryStore Phase 5 Component 4's dashboard
    already reads from (used automatically by /api/dashboard/history,
    /api/dashboard/metrics, /api/dashboard/charts) - the identical
    zero-modification integration pattern Phase 5 Component 5 (AI Builder
    Reliability) established for the same dashboard.
    """
    from core.observability import get_default_execution_history

    review_time = None
    if request.get("resolved_at") is not None:
        review_time = request["resolved_at"] - request["created_at"]
    get_default_execution_history().record_execution(
        pipeline_name="human_approval", venture_id=request["venture_id"], status=request["status"],
        execution_time_seconds=review_time, confidence_score=None, competitor_count=None,
        metadata={"stage": request["stage"], "policy": request["policy"], "approval_id": request["approval_id"]},
        execution_id=request["approval_id"],
    )


class ApprovalStatus:
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"
    EXPIRED = "expired"
    ESCALATED = "escalated"
    AUTO_APPROVED = "auto_approved"


TERMINAL_STATUSES = {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED, ApprovalStatus.EXPIRED, ApprovalStatus.AUTO_APPROVED}
ACTIONABLE_STATUSES = {ApprovalStatus.PENDING, ApprovalStatus.CHANGES_REQUESTED, ApprovalStatus.ESCALATED}


class ApprovalStage:
    RESEARCH = "research"
    DECISION = "decision"
    MVP = "mvp"
    AI_BUILDER = "ai_builder"
    DEPLOYMENT = "deployment"
    GROWTH = "growth"


STAGES = (
    ApprovalStage.RESEARCH, ApprovalStage.DECISION, ApprovalStage.MVP,
    ApprovalStage.AI_BUILDER, ApprovalStage.DEPLOYMENT, ApprovalStage.GROWTH,
)


class ApprovalPolicyType:
    AUTO = "auto_approval"
    MANUAL = "manual_approval"
    MULTI_STEP = "multi_step_approval"
    ROLE_BASED = "role_based_approval"


# Pure data - deterministic, no LLM. A founder journey's default review
# posture per stage: research is low-risk and auto-approved; everything that
# spends real money or ships real code needs a human, with deployment
# specifically requiring a founder/admin (role-based), and AI Builder
# requiring two independent approvals given it's the highest blast-radius
# stage (matches Phase 4 Component 2's own "highest blast-radius phase"
# framing for the product-build stage).
_DEFAULT_POLICY_TABLE: dict[str, dict[str, Any]] = {
    ApprovalStage.RESEARCH: {"policy": ApprovalPolicyType.AUTO},
    ApprovalStage.DECISION: {"policy": ApprovalPolicyType.MANUAL, "required_steps": 1, "timeout_seconds": 86400.0, "escalate_on_timeout": True},
    ApprovalStage.MVP: {"policy": ApprovalPolicyType.MANUAL, "required_steps": 1, "timeout_seconds": 86400.0, "escalate_on_timeout": True},
    ApprovalStage.AI_BUILDER: {"policy": ApprovalPolicyType.MULTI_STEP, "required_steps": 2, "timeout_seconds": 172800.0, "escalate_on_timeout": True},
    ApprovalStage.DEPLOYMENT: {"policy": ApprovalPolicyType.ROLE_BASED, "required_role": Role.FOUNDER, "required_steps": 1, "timeout_seconds": 43200.0, "escalate_on_timeout": True},
    ApprovalStage.GROWTH: {"policy": ApprovalPolicyType.MANUAL, "required_steps": 1},
}

_policy_table: dict[str, dict[str, Any]] = dict(_DEFAULT_POLICY_TABLE)
_policy_lock = threading.RLock()


def get_approval_policy_table() -> dict[str, dict[str, Any]]:
    with _policy_lock:
        return dict(_policy_table)


def set_approval_policy_table(table: dict[str, dict[str, Any]]) -> None:
    global _policy_table
    with _policy_lock:
        _policy_table = dict(table)


def reset_approval_policy_table() -> None:
    global _policy_table
    with _policy_lock:
        _policy_table = dict(_DEFAULT_POLICY_TABLE)


def resolve_policy_for_stage(stage: str) -> dict[str, Any]:
    with _policy_lock:
        return dict(_policy_table.get(stage, {"policy": ApprovalPolicyType.MANUAL, "required_steps": 1}))


def authorize_reviewer(request: dict[str, Any], reviewer_id: str) -> bool:
    """Pure authorization check, deterministic given the request + the
    TeamStore's current state. Role-based requests require the reviewer's
    role to match required_role (a founder can always act, mirroring
    real-world override authority). Requests with explicit
    assigned_reviewers require membership in that list. An open request
    (neither) can be acted on by anyone - matches "manual_approval" with no
    specific reviewer assignment.
    """
    if request.get("policy") == ApprovalPolicyType.ROLE_BASED and request.get("required_role"):
        from core.collaboration import get_default_team_store

        member = get_default_team_store().get_member(reviewer_id)
        if member is None:
            return False
        return member["role"] in (request["required_role"], Role.FOUNDER)
    assigned = request.get("assigned_reviewers") or []
    if assigned:
        return reviewer_id in assigned
    return True


class ApprovalStore:
    """Thread-safe Human Approval Engine: approval queue, decision audit
    trail, multi-step approval progression, timeout/expiration/escalation.
    Every mutation records a core.collaboration ActivityFeedStore entry and
    dispatches a core.notifications notification - composition over those
    already-built modules, never reimplemented here.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._requests: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []

    # ------------------------------------------------------------------ #
    # Creation
    # ------------------------------------------------------------------ #

    def create_request(
        self,
        venture_id: str,
        stage: str,
        payload: dict[str, Any],
        requested_by: str = "system",
        policy: str = ApprovalPolicyType.MANUAL,
        required_steps: int = 1,
        assigned_reviewers: list[str] | None = None,
        required_role: str | None = None,
        timeout_seconds: float | None = None,
        escalate_on_timeout: bool = False,
        escalation_reviewer: str | None = None,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            aid = approval_id or str(uuid.uuid4())
            is_auto = policy == ApprovalPolicyType.AUTO
            entry: dict[str, Any] = {
                "approval_id": aid, "venture_id": venture_id, "stage": stage, "payload": payload,
                "status": ApprovalStatus.AUTO_APPROVED if is_auto else ApprovalStatus.PENDING,
                "policy": policy, "requested_by": requested_by,
                "assigned_reviewers": list(assigned_reviewers or []), "required_role": required_role,
                "current_approval_step": required_steps if is_auto else 0, "required_steps": required_steps,
                "notes": [], "decision_history": [],
                "created_at": now, "updated_at": now,
                "timeout_seconds": timeout_seconds,
                "expires_at": (now + timeout_seconds) if (timeout_seconds and not is_auto) else None,
                "escalate_on_timeout": escalate_on_timeout, "escalation_reviewer": escalation_reviewer,
                "escalated_to": None,
                "resolved_at": now if is_auto else None,
            }
            if is_auto:
                entry["decision_history"].append({"reviewer": "system", "action": "auto_approved", "note": "policy: auto approval", "timestamp": now, "step": required_steps})
            self._requests[aid] = entry
            self._order.append(aid)

        get_default_activity_store().record_activity(
            venture_id=venture_id, actor_id=requested_by,
            action="approval_auto_approved" if is_auto else "approval_requested",
            target_type="approval", target_id=aid, details={"stage": stage},
        )
        if not is_auto:
            for reviewer in (assigned_reviewers or []):
                dispatch(
                    recipient_id=reviewer, type=NotificationType.APPROVAL_REQUESTED, title=f"Approval requested: {stage}",
                    message=f"Review requested for {stage} on venture {venture_id}", venture_id=venture_id,
                    related_type="approval", related_id=aid,
                )
        result = self.get_request(aid)
        _publish("auto_approved" if is_auto else "requested", venture_id, aid, stage)
        if is_auto:
            _record_resolved_execution(result)
        return result

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #

    def get_request(self, approval_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._requests.get(approval_id)
            return json.loads(json.dumps(entry)) if entry is not None else None

    def list_requests(
        self,
        venture_id: str | None = None,
        stage: str | None = None,
        status: str | None = None,
        search: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        with self._lock:
            items = [json.loads(json.dumps(self._requests[aid])) for aid in reversed(self._order)]
        if venture_id is not None:
            items = [r for r in items if r["venture_id"] == venture_id]
        if stage is not None:
            items = [r for r in items if r["stage"] == stage]
        if status is not None:
            items = [r for r in items if r["status"] == status]
        if search:
            needle = search.lower()
            items = [r for r in items if needle in r["stage"].lower() or needle in r["venture_id"].lower() or needle in r["approval_id"].lower()]
        items = items[offset:]
        if limit is not None:
            items = items[:limit]
        return items

    def list_pending(self, venture_id: str | None = None, stage: str | None = None) -> list[dict[str, Any]]:
        return self.list_requests(venture_id=venture_id, stage=stage, status=ApprovalStatus.PENDING)

    def decision_history(self, approval_id: str) -> list[dict[str, Any]]:
        request = self.get_request(approval_id)
        return request["decision_history"] if request else []

    def review_time_seconds(self, approval_id: str) -> float | None:
        request = self.get_request(approval_id)
        if request is None or request["resolved_at"] is None:
            return None
        return request["resolved_at"] - request["created_at"]

    # ------------------------------------------------------------------ #
    # Decisions
    # ------------------------------------------------------------------ #

    def add_note(self, approval_id: str, author: str, note: str) -> bool:
        with self._lock:
            request = self._requests.get(approval_id)
            if request is None:
                return False
            request["notes"].append({"author": author, "note": note, "timestamp": time.time()})
            request["updated_at"] = time.time()
            return True

    def approve(self, approval_id: str, reviewer: str, note: str = "") -> dict[str, Any] | None:
        with self._lock:
            request = self._requests.get(approval_id)
            if request is None or request["status"] not in ACTIONABLE_STATUSES:
                return None
            if not authorize_reviewer(request, reviewer):
                raise PermissionError(f"reviewer '{reviewer}' is not authorized to act on approval {approval_id}")
            request["current_approval_step"] += 1
            now = time.time()
            request["decision_history"].append({"reviewer": reviewer, "action": "approved", "note": note, "timestamp": now, "step": request["current_approval_step"]})
            request["updated_at"] = now
            fully_approved = request["current_approval_step"] >= request["required_steps"]
            if fully_approved:
                request["status"] = ApprovalStatus.APPROVED
                request["resolved_at"] = now
            venture_id, stage, requested_by, status = request["venture_id"], request["stage"], request["requested_by"], request["status"]

        get_default_activity_store().record_activity(
            venture_id=venture_id, actor_id=reviewer, action="approval_step_approved", target_type="approval", target_id=approval_id, details={"stage": stage, "status": status},
        )
        if status == ApprovalStatus.APPROVED:
            dispatch(
                recipient_id=requested_by, type=NotificationType.APPROVED, title=f"{stage} approved",
                message=note, venture_id=venture_id, related_type="approval", related_id=approval_id,
            )
        result = self.get_request(approval_id)
        _publish("step_approved" if status != ApprovalStatus.APPROVED else "approved", venture_id, approval_id, stage, {"reviewer": reviewer})
        if status == ApprovalStatus.APPROVED:
            _record_resolved_execution(result)
        return result

    def reject(self, approval_id: str, reviewer: str, note: str = "") -> dict[str, Any] | None:
        with self._lock:
            request = self._requests.get(approval_id)
            if request is None or request["status"] not in ACTIONABLE_STATUSES:
                return None
            if not authorize_reviewer(request, reviewer):
                raise PermissionError(f"reviewer '{reviewer}' is not authorized to act on approval {approval_id}")
            now = time.time()
            request["status"] = ApprovalStatus.REJECTED
            request["resolved_at"] = now
            request["decision_history"].append({"reviewer": reviewer, "action": "rejected", "note": note, "timestamp": now, "step": request["current_approval_step"]})
            request["updated_at"] = now
            venture_id, stage, requested_by = request["venture_id"], request["stage"], request["requested_by"]

        get_default_activity_store().record_activity(
            venture_id=venture_id, actor_id=reviewer, action="approval_rejected", target_type="approval", target_id=approval_id, details={"stage": stage},
        )
        dispatch(
            recipient_id=requested_by, type=NotificationType.REJECTED, title=f"{stage} rejected",
            message=note, venture_id=venture_id, related_type="approval", related_id=approval_id,
        )
        result = self.get_request(approval_id)
        _publish("rejected", venture_id, approval_id, stage, {"reviewer": reviewer})
        _record_resolved_execution(result)
        return result

    def request_changes(self, approval_id: str, reviewer: str, note: str = "") -> dict[str, Any] | None:
        with self._lock:
            request = self._requests.get(approval_id)
            if request is None or request["status"] not in ACTIONABLE_STATUSES:
                return None
            if not authorize_reviewer(request, reviewer):
                raise PermissionError(f"reviewer '{reviewer}' is not authorized to act on approval {approval_id}")
            now = time.time()
            request["status"] = ApprovalStatus.CHANGES_REQUESTED
            request["current_approval_step"] = 0
            request["decision_history"].append({"reviewer": reviewer, "action": "changes_requested", "note": note, "timestamp": now, "step": 0})
            request["updated_at"] = now
            venture_id, stage, requested_by = request["venture_id"], request["stage"], request["requested_by"]

        get_default_activity_store().record_activity(
            venture_id=venture_id, actor_id=reviewer, action="approval_changes_requested", target_type="approval", target_id=approval_id, details={"stage": stage},
        )
        dispatch(
            recipient_id=requested_by, type=NotificationType.CHANGES_REQUESTED, title=f"{stage} needs changes",
            message=note, venture_id=venture_id, related_type="approval", related_id=approval_id,
        )
        result = self.get_request(approval_id)
        _publish("changes_requested", venture_id, approval_id, stage, {"reviewer": reviewer})
        return result

    def resubmit(self, approval_id: str, payload: dict[str, Any], resubmitted_by: str = "system") -> dict[str, Any] | None:
        with self._lock:
            request = self._requests.get(approval_id)
            if request is None or request["status"] != ApprovalStatus.CHANGES_REQUESTED:
                return None
            now = time.time()
            request["payload"] = payload
            request["status"] = ApprovalStatus.PENDING
            request["decision_history"].append({"reviewer": resubmitted_by, "action": "resubmitted", "note": "", "timestamp": now, "step": 0})
            request["updated_at"] = now
            if request["timeout_seconds"]:
                request["expires_at"] = now + request["timeout_seconds"]
            venture_id, stage, assigned = request["venture_id"], request["stage"], request["assigned_reviewers"]

        get_default_activity_store().record_activity(
            venture_id=venture_id, actor_id=resubmitted_by, action="approval_resubmitted", target_type="approval", target_id=approval_id, details={"stage": stage},
        )
        for reviewer in assigned:
            dispatch(
                recipient_id=reviewer, type=NotificationType.APPROVAL_REQUESTED, title=f"Resubmitted for review: {stage}",
                message="Updated submission ready for re-review", venture_id=venture_id, related_type="approval", related_id=approval_id,
            )
        result = self.get_request(approval_id)
        _publish("resubmitted", venture_id, approval_id, stage, {"resubmitted_by": resubmitted_by})
        return result

    def auto_approve(self, approval_id: str, note: str = "policy: auto-approved") -> dict[str, Any] | None:
        with self._lock:
            request = self._requests.get(approval_id)
            if request is None or request["status"] not in ACTIONABLE_STATUSES:
                return None
            now = time.time()
            request["status"] = ApprovalStatus.AUTO_APPROVED
            request["current_approval_step"] = request["required_steps"]
            request["resolved_at"] = now
            request["decision_history"].append({"reviewer": "system", "action": "auto_approved", "note": note, "timestamp": now, "step": request["required_steps"]})
            request["updated_at"] = now
            venture_id, stage, requested_by = request["venture_id"], request["stage"], request["requested_by"]

        get_default_activity_store().record_activity(
            venture_id=venture_id, actor_id="system", action="approval_auto_approved", target_type="approval", target_id=approval_id, details={"stage": stage},
        )
        dispatch(
            recipient_id=requested_by, type=NotificationType.APPROVED, title=f"{stage} auto-approved",
            message=note, venture_id=venture_id, related_type="approval", related_id=approval_id,
        )
        result = self.get_request(approval_id)
        _publish("auto_approved", venture_id, approval_id, stage)
        _record_resolved_execution(result)
        return result

    def escalate(self, approval_id: str, escalate_to: str, note: str = "") -> dict[str, Any] | None:
        with self._lock:
            request = self._requests.get(approval_id)
            if request is None or request["status"] not in ACTIONABLE_STATUSES:
                return None
            now = time.time()
            request["status"] = ApprovalStatus.ESCALATED
            request["escalated_to"] = escalate_to
            if escalate_to not in request["assigned_reviewers"]:
                request["assigned_reviewers"].append(escalate_to)
            request["decision_history"].append({"reviewer": "system", "action": "escalated", "note": note, "timestamp": now, "step": request["current_approval_step"]})
            request["updated_at"] = now
            venture_id, stage = request["venture_id"], request["stage"]

        get_default_activity_store().record_activity(
            venture_id=venture_id, actor_id="system", action="approval_escalated", target_type="approval", target_id=approval_id, details={"stage": stage, "escalated_to": escalate_to},
        )
        dispatch(
            recipient_id=escalate_to, type=NotificationType.APPROVAL_REQUESTED, title=f"Escalated for review: {stage}",
            message=note or "This approval was escalated to you", venture_id=venture_id, related_type="approval", related_id=approval_id,
        )
        result = self.get_request(approval_id)
        _publish("escalated", venture_id, approval_id, stage, {"escalated_to": escalate_to})
        return result

    def expire_if_due(self, approval_id: str, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        with self._lock:
            request = self._requests.get(approval_id)
            if request is None or request["status"] not in (ApprovalStatus.PENDING, ApprovalStatus.CHANGES_REQUESTED):
                return False
            if request["expires_at"] is None or now < request["expires_at"]:
                return False
            escalate_on_timeout, escalation_reviewer = request["escalate_on_timeout"], request["escalation_reviewer"]

        if escalate_on_timeout and escalation_reviewer:
            self.escalate(approval_id, escalation_reviewer, note="auto-escalated: approval timed out")
            return True

        with self._lock:
            request = self._requests.get(approval_id)
            if request is None or request["status"] not in (ApprovalStatus.PENDING, ApprovalStatus.CHANGES_REQUESTED):
                return False
            request["status"] = ApprovalStatus.EXPIRED
            request["resolved_at"] = now
            request["decision_history"].append({"reviewer": "system", "action": "expired", "note": "approval timed out", "timestamp": now, "step": request["current_approval_step"]})
            request["updated_at"] = now
            venture_id, stage = request["venture_id"], request["stage"]

        get_default_activity_store().record_activity(
            venture_id=venture_id, actor_id="system", action="approval_expired", target_type="approval", target_id=approval_id, details={"stage": stage},
        )
        _publish("expired", venture_id, approval_id, stage)
        _record_resolved_execution(self.get_request(approval_id))
        return True

    def sweep_expirations(self, now: float | None = None) -> int:
        with self._lock:
            candidate_ids = [
                aid for aid, r in self._requests.items()
                if r["status"] in (ApprovalStatus.PENDING, ApprovalStatus.CHANGES_REQUESTED) and r["expires_at"] is not None
            ]
        return sum(1 for aid in candidate_ids if self.expire_if_due(aid, now=now))

    # ------------------------------------------------------------------ #
    # Housekeeping
    # ------------------------------------------------------------------ #

    def clear(self) -> None:
        with self._lock:
            self._requests.clear()
            self._order.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._requests)

    def save_to_disk(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            path.write_text(json.dumps({"requests": self._requests, "order": self._order}, indent=2), encoding="utf-8")

    def load_from_disk(self, path: str | Path) -> bool:
        path = Path(path)
        if not path.exists():
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        with self._lock:
            self._requests = data.get("requests", {})
            self._order = data.get("order", [])
        return True


_default_store = ApprovalStore()


def get_default_approval_store() -> ApprovalStore:
    return _default_store


def set_default_approval_store(store: ApprovalStore) -> None:
    global _default_store
    _default_store = store


def reset_default_approval_store() -> None:
    global _default_store
    _default_store = ApprovalStore()
