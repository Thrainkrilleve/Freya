from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple


ANY = "*"


@dataclass(frozen=True)
class PolicyDecision:
    """
    The answer to "may this subject perform this action?".
    """

    allow: bool
    reason: str
    revision: Optional[int] = None
    grant_epoch: Optional[int] = None
    obligations: Tuple[str, ...] = ()
    expires_at: Optional[str] = None


class SqlitePolicyEngine:
    """
    Deterministic, deny-by-default policy evaluator for the alpha.

    A request is allowed only when at least one allow rule matches
    and no deny rule matches. Rules match on an exact subject and
    capability, or on "*". Every rule change bumps the policy
    revision, and each decision reports the revision it used.

    Errors reading policy state are raised to the caller, which must
    treat them as a denial.
    """

    def __init__(self, store):
        self._store = store

    @staticmethod
    def _normalize(value: str) -> str:
        return value.strip().lower()

    def state(self) -> Tuple[int, int]:
        """
        Return the current (revision, grant_epoch).
        """
        row = self._store.execute(
            "SELECT revision, grant_epoch FROM policy_state WHERE id = 1"
        ).fetchone()
        return row["revision"], row["grant_epoch"]

    def decide(
        self,
        subject: str,
        action: str,
        resource: Any = None,
        environment: Any = None,
    ) -> PolicyDecision:
        subject = self._normalize(subject)
        action = self._normalize(action)

        # The revision recorded with the decision must be the one the
        # rules were read under. The two are separate reads on a shared
        # connection, and a rule change (BEGIN IMMEDIATE) can land in
        # between, so the state is read on both sides and the rules
        # read again if it moved. Three tries cover any realistic
        # burst of owner changes; the last read stands either way.
        for _ in range(3):
            revision, grant_epoch = self.state()

            rules = self._store.execute(
                """
                SELECT effect, reason
                FROM policy_rule
                WHERE subject IN (?, ?)
                  AND capability IN (?, ?)
                ORDER BY rule_id
                """,
                (subject, ANY, action, ANY),
            ).fetchall()

            if self.state() == (revision, grant_epoch):
                break

        denies = [r for r in rules if r["effect"] == "deny"]
        allows = [r for r in rules if r["effect"] == "allow"]

        if denies:
            allow, reason = False, denies[0]["reason"]
        elif allows:
            allow, reason = True, allows[0]["reason"]
        else:
            allow, reason = False, "No policy rule allows this action."

        return PolicyDecision(
            allow=allow,
            reason=reason,
            revision=revision,
            grant_epoch=grant_epoch,
        )

    def add_rule(
        self,
        subject: str,
        capability: str,
        effect: str,
        reason: str,
    ) -> int:
        """
        Add a rule and bump the policy revision. Returns the rule ID.
        """
        subject = self._normalize(subject)
        capability = self._normalize(capability)

        if not subject or not capability:
            raise ValueError("Rule subject and capability are required.")

        if effect not in ("allow", "deny"):
            raise ValueError("Rule effect must be 'allow' or 'deny'.")

        if not reason.strip():
            raise ValueError("Rule reason is required.")

        with self._store.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO policy_rule
                    (subject, capability, effect, reason, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    subject,
                    capability,
                    effect,
                    reason.strip(),
                    datetime.now(timezone.utc).isoformat(
                        timespec="milliseconds"
                    ),
                ),
            )
            connection.execute(
                "UPDATE policy_state SET revision = revision + 1 "
                "WHERE id = 1"
            )
            return cursor.lastrowid

    def remove_rule(self, rule_id: int) -> bool:
        """
        Remove a rule. Removing a rule can withdraw authority, so it
        bumps the grant epoch as well as the revision.
        """
        with self._store.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM policy_rule WHERE rule_id = ?",
                (rule_id,),
            )
            if cursor.rowcount == 0:
                return False

            connection.execute(
                "UPDATE policy_state "
                "SET revision = revision + 1, "
                "grant_epoch = grant_epoch + 1 "
                "WHERE id = 1"
            )
            return True

    def list_rules(self) -> List[dict]:
        rows = self._store.execute(
            """
            SELECT rule_id, subject, capability, effect, reason, created_at
            FROM policy_rule
            ORDER BY rule_id
            """
        ).fetchall()
        return [dict(row) for row in rows]


    def apply_seed(
        self,
        name: str,
        subject: str,
        capabilities,
        reason: str,
    ) -> bool:
        """
        Apply a named set of default allow rules exactly once.

        A seed that has already been applied is never applied again,
        so rules the owner later removed are not silently restored.
        Returns True when the seed was applied by this call.
        """
        subject = self._normalize(subject)

        with self._store.transaction() as connection:
            applied = connection.execute(
                "SELECT 1 FROM policy_seed WHERE name = ?",
                (name,),
            ).fetchone()

            if applied is not None:
                return False

            now = datetime.now(timezone.utc).isoformat(
                timespec="milliseconds"
            )
            inserted = 0

            for capability in capabilities:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO policy_rule
                        (subject, capability, effect, reason, created_at)
                    VALUES (?, ?, 'allow', ?, ?)
                    """,
                    (
                        subject,
                        self._normalize(capability),
                        reason,
                        now,
                    ),
                )
                inserted += cursor.rowcount

            if inserted:
                connection.execute(
                    "UPDATE policy_state SET revision = revision + 1 "
                    "WHERE id = 1"
                )

            connection.execute(
                "INSERT INTO policy_seed (name, applied_at) VALUES (?, ?)",
                (name, now),
            )

        return True


ALPHA_DEFAULT_REASON = "Alpha default: local owner may use this capability."


def seed_alpha_policy(engine, name, actor, capabilities) -> bool:
    """
    Allow the local owner to use a default set of capabilities, once
    per named set.
    """
    return engine.apply_seed(
        name,
        actor,
        capabilities,
        ALPHA_DEFAULT_REASON,
    )
