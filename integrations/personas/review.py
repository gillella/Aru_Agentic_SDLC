"""Validate one current kernel assignment; never select or mutate authority.

Both the proposed assignment and the fresh snapshot are trusted Driver inputs.
This module does not authenticate GitHub or elevate JSON into kernel evidence.
"""
from dataclasses import dataclass, replace
import re

from .classify import TaskRequest, derive_risk_tier
from .errors import (ExternalFirstError, HeadMismatchError, ReviewAuthorityError,
                     ReviewIndependenceError)
from .lineage import AuthorIdentity, extend_history
from .registry import persona

MUTATES_KERNEL_AUTHORITY = False


@dataclass(frozen=True)
class ReviewAssignment:
    repo: str
    pr: int
    head: str
    issue: int
    risk_tier: int
    authority: str
    reviewer_persona: str
    reviewer_actor: str
    reviewer_account: str
    authors: tuple[AuthorIdentity, ...]
    touches: tuple[str, ...]
    # Exact-head output of the canonical helper, reread by the Driver.
    authority_source: str
    external_first_released: bool = False
    external_first_reason: str = ""

    def to_dict(self):
        return {**{k: v for k, v in vars(self).items() if k not in {"authors", "touches"}},
                "authors": [a.to_dict() for a in self.authors], "touches": list(self.touches)}


def validate_assignment(assignment, current_head, *, current_assignment):
    if not isinstance(assignment, ReviewAssignment) or assignment != current_assignment:
        raise ReviewAuthorityError("proposed assignment differs from fresh kernel authority")
    if not re.fullmatch(r"[0-9a-f]{40}", current_head or "") or assignment.head != current_head:
        raise HeadMismatchError("review must bind the full current head")
    if (assignment.external_first_released is not True or
            not assignment.external_first_reason.strip() or not assignment.authority_source.strip()):
        raise ExternalFirstError("kernel external-first release evidence is required")
    if type(assignment.pr) is not int or assignment.pr < 1 or not assignment.authors:
        raise ReviewAuthorityError("review requires a PR and complete author history")
    if type(assignment.risk_tier) is not int or not 0 <= assignment.risk_tier <= 3:
        raise ReviewAuthorityError("invalid review risk tier")
    if assignment.risk_tier < derive_risk_tier(assignment.touches):
        raise ReviewAuthorityError("review risk below scope floor")
    reviewer = persona(assignment.reviewer_persona)
    identity = AuthorIdentity(reviewer.id, assignment.reviewer_account, assignment.reviewer_actor)
    if (not reviewer.performs("code_reviewer") or not reviewer.reviews_at(assignment.risk_tier)
            or assignment.authority != reviewer.lineage):
        raise ReviewAuthorityError("bound reviewer lacks authority for this role/risk/family")
    for author in assignment.authors:
        if (identity.family == author.family or identity.account_id == author.account_id or
                identity.actor.casefold() == author.actor.casefold()):
            raise ReviewIndependenceError("reviewer overlaps cumulative author family/account/actor")
    return assignment


def plan_review(assignment, current_head, binding, context, now=None, *, current_assignment):
    from .resolve import _resolve
    validate_assignment(assignment, current_head, current_assignment=current_assignment)
    if context.head != current_head or context.pr != assignment.pr:
        raise HeadMismatchError("review prompt context differs from assigned PR/head")
    bound_account = binding.require(assignment.reviewer_account)
    bound_account.require_project(assignment.repo)
    # Pin BOTH model persona and exact assigned account. No account fallback.
    bound = replace(binding, accounts=(bound_account,))
    request = TaskRequest(project=assignment.repo, issue=assignment.issue,
                          task_class="code_review", risk_tier=assignment.risk_tier,
                          touches=assignment.touches, persona_override=assignment.reviewer_persona,
                          author_history=assignment.authors, actor=assignment.reviewer_actor)
    plan = _resolve(request, bound, context, now)
    return replace(plan, review_assignment=assignment.to_dict(), digest="")


def require_author_continuity(history, next_persona, account_id, actor, handoff_reason=""):
    """An explicitly authorized continuation appends every contributor, never resets."""
    return extend_history(history, next_persona, account_id, actor, handoff_reason)
