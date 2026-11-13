import hashlib
import inspect
import json
import logging
import threading
import uuid
from dataclasses import dataclass
from typing import Any, List, Optional

from freya.core.capabilities.execution_context import (enter_broker_context, exit_broker_context)
from freya.core.capabilities.capability import (
    CapabilityStatus,
)
from freya.core.policy.engine import (
    PolicyDecision,
)


logger = logging.getLogger(__name__)

# Audit records that could not be written after the fact (a denial, or
# the outcome of an action that already ran) are kept here and written
# before anything else may run. The limit bounds memory while storage
# is down; nothing can be dispatched in that state anyway.
PENDING_AUDIT_LIMIT = 256


@dataclass(frozen=True)
class Principal:
    """
    Who an invocation is performed for. Core decides this when it
    builds the broker; callers of invoke() cannot supply or change it.
    """

    actor: str
    client: str
    domain: str


# Until authenticated sessions exist, everything the CLI does is
# attributed to the single local owner.
LOCAL_OWNER = Principal(
    actor="local-owner",
    client="cli",
    domain="local",
)


@dataclass
class BrokerResult:
    """
    Result of asking the broker to invoke a capability.

    outcome is one of: succeeded, failed-known, unknown-outcome,
    denied, unavailable, or streaming. When it is streaming, result
    is an iterator and the final outcome is written to the audit log
    once that iterator finishes, fails, or is closed.
    """

    success: bool
    capability_name: str
    operation_id: str
    outcome: str
    provider: Optional[str] = None
    result: Any = None
    error: Optional[str] = None
    # False when the record of this decision or outcome could not be
    # written yet. The broker keeps it and refuses new dispatches
    # until it has been written. For a stream this reflects only the
    # pre-execution record; the outcome is written when it ends.
    audit_recorded: bool = True


def _arguments_digest(args, kwargs) -> str:
    canonical = json.dumps(
        [list(args), kwargs],
        sort_keys=True,
        default=repr,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CapabilityBroker:
    """
    The single path for invoking a capability.

    Each invocation is, in order: authorized by policy (deny by
    default), checked for availability, recorded in the audit log,
    executed, and has its outcome recorded. If policy cannot be read
    or the pre-execution audit record cannot be written, the
    capability is not executed.

    Audit writes fall into two classes:

    - Required before acting (the dispatch record). If it cannot be
      written the capability does not run.
    - Required, but after the fact (a denial, an unavailability
      decision, an outcome). The decision has already been made or
      the action has already happened, so failing to write cannot
      undo it. These are never dropped silently: the failure is
      logged, reported on the result, and the record is kept and
      written before any further capability may be dispatched.

    Best-effort telemetry (checkpoints) lives in the audit log itself
    and never affects whether a capability runs.
    """

    def __init__(
        self,
        registry,
        policy,
        audit,
        principal: Principal = LOCAL_OWNER,
    ):
        self._registry = registry
        self._policy = policy
        self._audit = audit
        self._principal = principal

        self._pending_lock = threading.Lock()
        self._pending_audit: List[dict] = []
        self._lost_audit_records = 0

    @property
    def pending_audit_records(self) -> int:
        """Audit records waiting to be written; while nonzero, nothing is dispatched."""
        with self._pending_lock:
            return len(self._pending_audit)

    @property
    def lost_audit_records(self) -> int:
        """Records dropped because the pending queue was full."""
        with self._pending_lock:
            return self._lost_audit_records

    def exists(self, name: str) -> bool:
        return self._registry.exists(name)

    def invoke(
        self,
        name: str,
        *args,
        **kwargs,
    ) -> BrokerResult:
        operation_id = uuid.uuid4().hex

        capability = self._registry.get(name)
        capability_name = (
            capability.name
            if capability is not None
            else name.strip().lower()
        )
        provider = (
            capability.provider
            if capability is not None
            else None
        )

        def result(outcome, error=None, value=None, recorded=True):
            return BrokerResult(
                success=outcome in ("succeeded", "streaming"),
                capability_name=capability_name,
                operation_id=operation_id,
                outcome=outcome,
                provider=provider,
                result=value,
                error=error,
                audit_recorded=recorded,
            )

        if capability is None:
            decision = PolicyDecision(
                allow=False,
                reason="Capability is not registered.",
            )
        else:
            try:
                decision = self._policy.decide(
                    self._principal.actor,
                    capability_name,
                )
            except Exception:
                decision = PolicyDecision(
                    allow=False,
                    reason="Policy state is unavailable.",
                )

        event = {
            "operation_id": operation_id,
            "actor": self._principal.actor,
            "client": self._principal.client,
            "domain": self._principal.domain,
            "capability": capability_name,
            "provider": provider,
            "decision": "allow" if decision.allow else "deny",
            "decision_reason": decision.reason,
            "policy_revision": decision.revision,
            "grant_epoch": decision.grant_epoch,
            "arguments_digest": _arguments_digest(args, kwargs),
        }

        if not decision.allow:
            recorded = self._record_after_the_fact(
                event,
                kind="decision",
                outcome="denied",
            )
            return result(
                "denied",
                error=f"Not authorized: {decision.reason}",
                recorded=recorded,
            )

        status = self._registry.get_status(capability_name)

        if status != CapabilityStatus.AVAILABLE:
            error = (
                "Capability is not available. "
                f"Current status: {status.value}"
            )
            recorded = self._record_after_the_fact(
                event,
                kind="decision",
                outcome="unavailable",
                error_summary=error,
            )
            return result("unavailable", error=error, recorded=recorded)

        if not self._flush_pending_audit():
            return result(
                "denied",
                error=(
                    "Not performed because earlier audit records "
                    "have not been written yet."
                ),
            )

        try:
            self._audit.record(kind="dispatch", **event)
        except Exception as exc:
            return result(
                "denied",
                error=(
                    "Not performed because the action could not be "
                    f"recorded in the audit log: {exc}"
                ),
            )

        token = enter_broker_context(self._principal, self._policy)
        try:
            execution = self._registry.execute(
                capability_name,
                *args,
                **kwargs,
            )
        finally:
            exit_broker_context(token)

        # A destructive action that raised may still have had an effect.
        failure = (
            "unknown-outcome"
            if capability.destructive
            else "failed-known"
        )

        if execution.success and inspect.isgenerator(
            execution.result
        ):
            # The work happens as the caller consumes the stream, so
            # the outcome is recorded when the stream ends.
            return result(
                "streaming",
                value=self._audited_stream(
                    execution.result,
                    event,
                    failure,
                ),
            )

        outcome = "succeeded" if execution.success else failure

        recorded = self._record_after_the_fact(
            event,
            kind="outcome",
            outcome=outcome,
            error_summary=execution.error,
        )

        return result(
            outcome,
            error=execution.error,
            value=execution.result,
            recorded=recorded,
        )

    def _audited_stream(self, stream, event, failure):
        outcome = failure
        error = "Stream closed before completion."

        try:
            yield from stream
            outcome, error = "succeeded", None
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            self._record_after_the_fact(
                event,
                kind="outcome",
                outcome=outcome,
                error_summary=error,
            )

    def _record_after_the_fact(self, event, **fields) -> bool:
        """
        Write a record of something that has already been decided or
        done. Returns False when it could not be written now; it is
        then queued and must be written before the next dispatch.
        """
        record = {**event, **fields}

        with self._pending_lock:
            # Keep the trail in order: older pending records go first.
            if self._write_pending_locked():
                try:
                    self._audit.record(**record)
                    return True
                except Exception as exc:
                    failure = exc
            else:
                failure = None

            self._queue_locked(record)

        logger.error(
            "Audit record for operation %s (%s, %s) could not be "
            "written and is queued; new dispatches are refused until "
            "it is written.%s",
            record["operation_id"],
            record["capability"],
            record["kind"],
            f" {type(failure).__name__}: {failure}"[:200] if failure else "",
        )
        return False

    def _flush_pending_audit(self) -> bool:
        """Write queued records. True when none remain."""
        with self._pending_lock:
            return self._write_pending_locked()

    def _write_pending_locked(self) -> bool:
        while self._pending_audit:
            try:
                self._audit.record(**self._pending_audit[0])
            except Exception:
                return False
            self._pending_audit.pop(0)
        return True

    def _queue_locked(self, record: dict) -> None:
        if len(self._pending_audit) >= PENDING_AUDIT_LIMIT:
            # Dropping is a loss of evidence, so it is loud. A lost
            # outcome is still closed out as unknown-outcome when the
            # audit log next starts, because its dispatch was written.
            self._lost_audit_records += 1
            logger.critical(
                "Audit queue full: record for operation %s (%s, %s) "
                "was lost (%d lost so far).",
                record["operation_id"],
                record["capability"],
                record["kind"],
                self._lost_audit_records,
            )
            return
        self._pending_audit.append(record)
