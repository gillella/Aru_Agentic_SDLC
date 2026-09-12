# 0001 - Scope is judged live at merge, not pinned at promotion

Date: 2026-09-12. Status: accepted. Supersedes the `ready:<digest>` promotion pin.

Moved out of `docs/KERNEL-CONTRACT.md` when the contract was reduced to its
normative rules. The rule itself stays in the contract; this is why it is that way.

## Rationale, as the contract stated it

That is deliberate, and it rests on the approval. Promotion was previously pinned
by a digest, which refused a body edited afterwards and directed the operator to
return the issue to Backlog — a transition the kernel does not implement, and one
that is impossible once a pull request exists, because the issue is then In
Review and releasing the claim is refused. What the pin guarded is covered by the
merge gate itself: an approval of the exact head by an authorized account, over a
diff that lists every changed path. A widened scope is visible in the thing the
approver is reading.
