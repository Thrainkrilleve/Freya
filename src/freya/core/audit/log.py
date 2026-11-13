import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional


GENESIS_HASH = "0" * 64

logger = logging.getLogger(__name__)

HASHED_FIELDS = (
    "event_id",
    "operation_id",
    "recorded_at",
    "kind",
    "actor",
    "client",
    "domain",
    "capability",
    "provider",
    "target_ref",
    "decision",
    "decision_reason",
    "policy_revision",
    "grant_epoch",
    "outcome",
    "error_summary",
    "arguments_digest",
    "prev_hash",
)

ERROR_SUMMARY_LIMIT = 200


class AuditUnavailable(Exception):
    """
    The audit log cannot accept records. Callers must not perform the
    action they were about to record.
    """


@dataclass
class AuditVerification:
    ok: bool
    events_checked: int
    problems: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class AuditStartup:
    verification: AuditVerification
    reconciled: int = 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _describe(exc: BaseException) -> str:
    """
    A short description of a storage failure, safe to show the operator.
    Storage errors describe the database, never audited payloads.
    """
    return f"{type(exc).__name__}: {exc}"[:ERROR_SUMMARY_LIMIT]


def _row_hash(event: dict) -> str:
    canonical = json.dumps(
        {name: event[name] for name in HASHED_FIELDS},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SqliteAuditLog:
    """
    Append-only, hash-chained audit trail.

    Every row stores the hash of the row before it, so editing or
    removing a row in the middle is detectable. Checkpoints record the
    chain head; when a checkpoint sink outside this machine's control
    is configured, rewriting or truncating the log is detectable back
    to the last checkpoint. This does not prevent tampering by a host
    administrator.

    Payloads are never stored: arguments appear only as a digest.
    """

    def __init__(self, store, sink=None, checkpoint_interval=100):
        self._store = store
        self._sink = sink
        self._checkpoint_interval = checkpoint_interval

        # Until start() has verified the chain, nothing may be recorded.
        self.integrity_ok = False

    def start(self) -> AuditStartup:
        """
        Verify the log, close out operations that were interrupted
        before their outcome was recorded, and checkpoint.
        """
        self.integrity_ok = False

        try:
            verification = self.verify()
        except Exception as exc:
            # A log that cannot be read cannot be trusted: fail closed
            # rather than crash, so the operator is told why.
            return AuditStartup(
                verification=AuditVerification(
                    ok=False,
                    events_checked=0,
                    problems=[
                        "The audit log could not be read: "
                        f"{_describe(exc)}"
                    ],
                )
            )

        if not verification.ok:
            return AuditStartup(verification=verification)

        # record() refuses until integrity_ok is set, and
        # reconciliation has to write.
        self.integrity_ok = True

        try:
            reconciled = self.reconcile_interrupted()
        except Exception as exc:
            self.integrity_ok = False
            verification.ok = False
            verification.problems.append(
                "Interrupted operations could not be closed out: "
                f"{_describe(exc)}"
            )
            return AuditStartup(verification=verification)

        try:
            self.checkpoint()
        except Exception as exc:
            # The chain itself is intact and verified; a missing
            # checkpoint only weakens truncation evidence until the
            # next one is written.
            verification.warnings.append(
                f"Startup checkpoint could not be written: {_describe(exc)}"
            )

        return AuditStartup(
            verification=verification,
            reconciled=reconciled,
        )

    def record(
        self,
        *,
        operation_id: str,
        kind: str,
        actor: str,
        client: str,
        domain: str,
        capability: str,
        decision: str,
        decision_reason: str,
        provider: Optional[str] = None,
        target_ref: Optional[str] = None,
        policy_revision: Optional[int] = None,
        grant_epoch: Optional[int] = None,
        outcome: Optional[str] = None,
        error_summary: Optional[str] = None,
        arguments_digest: Optional[str] = None,
    ) -> int:
        """
        Durably append one event and return its ID.

        Raises AuditUnavailable when the log failed verification or
        the write does not succeed.
        """
        if not self.integrity_ok:
            raise AuditUnavailable(
                "The audit log has not passed integrity verification."
            )

        if error_summary is not None:
            error_summary = error_summary[:ERROR_SUMMARY_LIMIT]

        event = {
            "operation_id": operation_id,
            "recorded_at": _now(),
            "kind": kind,
            "actor": actor,
            "client": client,
            "domain": domain,
            "capability": capability,
            "provider": provider,
            "target_ref": target_ref,
            "decision": decision,
            "decision_reason": decision_reason,
            "policy_revision": policy_revision,
            "grant_epoch": grant_epoch,
            "outcome": outcome,
            "error_summary": error_summary,
            "arguments_digest": arguments_digest,
        }

        try:
            with self._store.transaction() as connection:
                head = connection.execute(
                    "SELECT event_id, row_hash FROM audit_event "
                    "ORDER BY event_id DESC LIMIT 1"
                ).fetchone()

                event["event_id"] = (head["event_id"] if head else 0) + 1
                event["prev_hash"] = (
                    head["row_hash"] if head else GENESIS_HASH
                )
                event["row_hash"] = _row_hash(event)

                columns = ("operation_id", "recorded_at", "kind", "actor", "client", "domain", "capability", "provider", "target_ref", "decision", "decision_reason", "policy_revision", "grant_epoch", "outcome", "error_summary", "arguments_digest", "event_id", "prev_hash", "row_hash")
                connection.execute(
                    "INSERT INTO audit_event (operation_id, recorded_at, kind, actor, client, domain, capability, provider, target_ref, decision, decision_reason, policy_revision, grant_epoch, outcome, error_summary, arguments_digest, event_id, prev_hash, row_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [event[name] for name in columns],
                )
        except Exception as exc:
            raise AuditUnavailable(
                f"The audit record could not be written: {exc}"
            ) from exc

        # The event is committed. A checkpoint is supplementary evidence,
        # so failing to write one must not be reported as a failure to
        # record the event: the caller would believe an action was not
        # logged when it was.
        try:
            self._checkpoint_if_due(event["event_id"])
        except Exception as exc:
            logger.warning(
                "Audit checkpoint after event %s could not be written: %s",
                event["event_id"],
                _describe(exc),
            )

        return event["event_id"]

    def reconcile_interrupted(self) -> int:
        """
        Record an unknown outcome for every dispatched operation that
        has no outcome row, for example after a crash mid-action.
        """
        rows = self._store.execute(
            """
            SELECT *
            FROM audit_event AS dispatched
            WHERE kind = 'dispatch'
              AND NOT EXISTS (
                  SELECT 1
                  FROM audit_event AS finished
                  WHERE finished.operation_id = dispatched.operation_id
                    AND finished.kind = 'outcome'
              )
            ORDER BY event_id
            """
        ).fetchall()

        for row in rows:
            self.record(
                operation_id=row["operation_id"],
                kind="outcome",
                actor=row["actor"],
                client=row["client"],
                domain=row["domain"],
                capability=row["capability"],
                provider=row["provider"],
                target_ref=row["target_ref"],
                decision=row["decision"],
                decision_reason=row["decision_reason"],
                policy_revision=row["policy_revision"],
                grant_epoch=row["grant_epoch"],
                outcome="unknown-outcome",
                error_summary=(
                    "Interrupted before an outcome was recorded."
                ),
                arguments_digest=row["arguments_digest"],
            )

        return len(rows)

    def head(self) -> dict:
        row = self._store.execute(
            "SELECT event_id, row_hash FROM audit_event "
            "ORDER BY event_id DESC LIMIT 1"
        ).fetchone()

        if row is None:
            return {"last_event_id": 0, "row_hash": GENESIS_HASH}

        return {
            "last_event_id": row["event_id"],
            "row_hash": row["row_hash"],
        }

    def checkpoint(self) -> Optional[dict]:
        """
        Record the current chain head locally and in the sink, if one
        is configured. Returns None when the head has not moved since
        the last checkpoint.

        A sink that cannot be reached does not stop Freya; the failure
        is recorded with the checkpoint and reported by verify().
        """
        checkpoint = self.head()

        latest = self._store.execute(
            "SELECT last_event_id, row_hash, sink_status "
            "FROM audit_checkpoint "
            "ORDER BY checkpoint_id DESC LIMIT 1"
        ).fetchone()

        if (
            latest is not None
            and latest["last_event_id"] == checkpoint["last_event_id"]
            and latest["row_hash"] == checkpoint["row_hash"]
            and latest["sink_status"] in (None, "written")
        ):
            return None

        checkpoint["created_at"] = _now()

        sink_name = None
        sink_status = None

        if self._sink is not None:
            sink_name = self._sink.name
            try:
                self._sink.write(dict(checkpoint))
                sink_status = "written"
            except Exception as exc:
                sink_status = f"failed: {exc}"[:ERROR_SUMMARY_LIMIT]

        with self._store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO audit_checkpoint
                    (created_at, last_event_id, row_hash,
                     sink, sink_status)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    checkpoint["created_at"],
                    checkpoint["last_event_id"],
                    checkpoint["row_hash"],
                    sink_name,
                    sink_status,
                ),
            )

        checkpoint["sink"] = sink_name
        checkpoint["sink_status"] = sink_status

        return checkpoint

    def _checkpoint_if_due(self, event_id: int) -> None:
        if not self._checkpoint_interval:
            return

        row = self._store.execute(
            "SELECT MAX(last_event_id) FROM audit_checkpoint"
        ).fetchone()

        if event_id - (row[0] or 0) >= self._checkpoint_interval:
            self.checkpoint()

    def verify(self) -> AuditVerification:
        """
        Recompute the hash chain and compare it with every checkpoint.
        """
        problems = []
        warnings = []
        hashes = {0: GENESIS_HASH}

        expected_id = 1
        previous_hash = GENESIS_HASH

        cursor = self._store.execute(
            "SELECT * FROM audit_event ORDER BY event_id"
        )

        for row in cursor.fetchall():
            event = dict(row)
            event_id = event["event_id"]

            if event_id != expected_id:
                problems.append(
                    f"Event {expected_id} is missing "
                    f"(found {event_id})."
                )

            if event["prev_hash"] != previous_hash:
                problems.append(
                    f"Event {event_id} does not link to the event "
                    "before it."
                )

            if _row_hash(event) != event["row_hash"]:
                problems.append(
                    f"Event {event_id} has been altered."
                )

            hashes[event_id] = event["row_hash"]
            previous_hash = event["row_hash"]
            expected_id = event_id + 1

        checkpoints = [
            ("Local checkpoint", dict(row))
            for row in self._store.execute(
                "SELECT last_event_id, row_hash FROM audit_checkpoint "
                "ORDER BY checkpoint_id"
            ).fetchall()
        ]

        if self._sink is not None:
            try:
                external = self._sink.read_latest()
            except Exception as exc:
                external = None
                warnings.append(
                    f"Checkpoint sink could not be read: {exc}"
                )

            if external is not None:
                checkpoints.append(("External checkpoint", external))

        for label, checkpoint in checkpoints:
            event_id = checkpoint.get("last_event_id")

            if event_id not in hashes:
                problems.append(
                    f"{label} refers to event {event_id}, which is "
                    "no longer in the log."
                )
            elif hashes[event_id] != checkpoint.get("row_hash"):
                problems.append(
                    f"{label} for event {event_id} does not match "
                    "the log."
                )

        failed = self._store.execute(
            "SELECT COUNT(*) FROM audit_checkpoint "
            "WHERE sink_status LIKE 'failed%'"
        ).fetchone()[0]

        if failed:
            warnings.append(
                f"{failed} checkpoint(s) could not be written to the "
                "checkpoint sink."
            )

        return AuditVerification(
            ok=not problems,
            events_checked=len(hashes) - 1,
            problems=problems,
            warnings=warnings,
        )
