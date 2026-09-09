"""Cumulative authorship supplied from trusted handoff and GitHub evidence."""
from dataclasses import dataclass
import re

from .errors import AuthorLineageError
from .registry import account, persona


@dataclass(frozen=True)
class AuthorIdentity:
    persona_id: str
    account_id: str
    actor: str

    def __post_init__(self):
        if persona(self.persona_id).lineage != account(self.account_id).lineage:
            raise AuthorLineageError("author persona and account families disagree")
        if not isinstance(self.actor, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*(?:\[bot\])?", self.actor):
            raise AuthorLineageError("author requires a literal trusted actor")

    @property
    def family(self):
        return persona(self.persona_id).lineage

    def to_dict(self):
        return {"persona_id": self.persona_id, "account_id": self.account_id,
                "actor": self.actor, "family": self.family}


def extend_history(history, selected, account_id, actor, handoff_reason=""):
    """Append; never replace earlier contributors when changing model or family."""
    current = AuthorIdentity(selected, account_id, actor)
    if history and current.family not in {a.family for a in history} and not handoff_reason.strip():
        raise AuthorLineageError("cross-family continuation requires an explicit handoff reason")
    return tuple(history) if current in history else (*history, current)
