"""
A record of play actions for one session.

It is not a database and not a second audit log. Each row is the action
name, the outcome, a fixed reason, and a digest of the arguments. The
plan's text is not a field, so a refusal can be recorded without it.

A caller may hand each row to the existing hash-chained audit log. That
write is the caller's. This record does not open the control database
and does not add a migration. ADR-015 is unchanged.
"""

import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class AuditRow:
    kind: str
    action: str
    outcome: str
    reason: str
    arguments_digest: str


def arguments_digest(arguments) -> str:
    """A digest of the declared arguments. The values are not stored beside it."""
    payload = json.dumps(dict(arguments), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ActionLog:
    """Append-only for one session. Rows are not edited and not dropped."""

    def __init__(self, sink=None):
        self._rows = []
        self._sink = sink

    def record(self, *, kind, action, outcome, reason, arguments_digest="") -> AuditRow:
        if not isinstance(reason, str) or "\n" in reason or len(reason) > 200:
            reason = "The plan was refused."
        row = AuditRow(
            kind=str(kind),
            action=str(action),
            outcome=str(outcome),
            reason=reason,
            arguments_digest=str(arguments_digest or ""),
        )
        self._rows.append(row)
        if self._sink is not None:
            try:
                self._sink(row)
            except Exception:
                # The row is already kept. Stopping play does not wait
                # on a sink, and a failed sink does not erase the row.
                pass
        return row

    def __iter__(self):
        return iter(self._rows)

    def __len__(self):
        return len(self._rows)

    def rows(self):
        return tuple(self._rows)

    def text(self) -> str:
        """Every stored field, for a test that checks a plan did not leak."""
        return json.dumps(
            [row.__dict__ for row in self._rows],
            ensure_ascii=False,
            sort_keys=True,
        )
