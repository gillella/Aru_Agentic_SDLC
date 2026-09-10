"""Cumulative authorship supplied from trusted handoff and GitHub evidence.

Two independent families are recorded for every contributor and neither
substitutes for the other:

* **family** is the access lineage - which harness and subscription produced the
  work, and therefore which quota and which credentials it drew on;
* **vendor** is who authored the model. Reaching an Anthropic model through
  Cursor or Antigravity keeps Claude authorship, so a configured cross-route
  persona can never become an independent reviewer of Claude's own work.
"""
from dataclasses import dataclass, field
import re

from .errors import AuthorLineageError
from .policy import PolicySnapshot, default_snapshot


@dataclass(frozen=True)
class AuthorIdentity:
    persona_id: str
    account_id: str
    actor: str
    #: The policy this identity is read from. Excluded from equality and from
    #: ``to_dict`` so recorded history compares by identity, not by policy.
    snapshot: PolicySnapshot = field(default_factory=default_snapshot,
                                     repr=False, compare=False)

    def __post_init__(self):
        if self.snapshot.persona(self.persona_id).lineage != \
                self.snapshot.account(self.account_id).lineage:
            raise AuthorLineageError("author persona and account families disagree")
        if not isinstance(self.actor, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*(?:\[bot\])?", self.actor):
            raise AuthorLineageError("author requires a literal trusted actor")

    @property
    def family(self):
        return self.snapshot.persona(self.persona_id).lineage

    @property
    def vendor(self):
        return self.snapshot.persona(self.persona_id).author_vendor

    def to_dict(self):
        return {"persona_id": self.persona_id, "account_id": self.account_id,
                "actor": self.actor, "family": self.family, "vendor": self.vendor}


def extend_history(history, selected, account_id, actor, handoff_reason="", snapshot=None):
    """Append; never replace earlier contributors when changing model or family."""
    current = AuthorIdentity(selected, account_id, actor,
                             snapshot=snapshot or default_snapshot())
    if history and current.family not in {a.family for a in history} and not handoff_reason.strip():
        raise AuthorLineageError("cross-family continuation requires an explicit handoff reason")
    return tuple(history) if current in history else (*history, current)
