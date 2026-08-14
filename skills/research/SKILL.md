---
name: research
description: Turns a bounded research board issue into a cited findings artifact with mechanical citation verification. Use when the user asks to research a question on the board, run a research issue, or produce a research artifact with citations.
triggers:
  - "research issue"
  - "run research"
  - "research this question"
  - "produce research findings"
do_not_trigger_for:
  - "implementing a feature or fix (use implement-next-issue)"
  - "filing an issue without researching (use create-github-issue)"
---

# Research Skill

Research is claimable board work. Findings live on the issue as a durable
artifact; chat is not the record. A research issue is **Done** only when the
artifact exists and `scripts/verify_citations.py` exits 0.

## Preconditions

1. Work from a tracked GitHub issue with `type:research` (or title prefix
   `research:`) and a bounded **Research Question**.
2. Claim it: `python3 "$ARU_SDLC_HOME/scripts/claim_issue.py" --issue <N> --agent <ID>`
3. Stay inside the issue's `touches:` (typically `docs/research/**` or
   issue-comment attachment only).

## Procedure

### 1. Bound the question

Restate the question, in-scope / out-of-scope, and stop conditions in one
short comment if the issue body is ambiguous. If a product decision is
missing, leave the issue blocked — do not invent scope.

### 2. Investigate and write findings

Produce a markdown artifact with this shape:

```markdown
# Research findings: <short title>

## Question
<exact question>

## Findings
1. <claim> ([source](https://...))
2. ...

## Citations
- https://example.com/paper
- arXiv:2605.22534
- https://doi.org/10.1234/example

## Repo code claims
<!-- Only for claims about *this* repository's code. Required date. -->
- path: scripts/foo.py — verified: YYYY-MM-DD
```

Rules:

- Every factual claim needs a citation identifier (URL, `arXiv:`, or DOI).
- Claims about this repository's code **must** include `verified: YYYY-MM-DD`.
- Do not invent citations. Prefer primary sources.

Write the file under `docs/research/issue-<N>-<slug>.md` when `touches:`
allows `docs/research/**`; otherwise keep it as a local file to attach via
comment.

### 3. Verify citations mechanically

```bash
python3 "$ARU_SDLC_HOME/scripts/verify_citations.py" docs/research/issue-<N>-<slug>.md
```

Exit 0 is required. Unresolvable citations fail the acceptance criteria —
fix or remove them and re-run. Do not mark Done on belief alone.

### 4. Attach the artifact to the issue

Direct `gh` is allowed here: there is no findings-attach helper yet, and this
is the sanctioned exception for posting the findings artifact (parallel to
implementation-plan comments).

```bash
gh issue comment <N> --body-file <FINDINGS_OR_SUMMARY_FILE>
```

If the findings live in-repo, the comment may summarize and link the path;
the path must exist on the branch that closes the issue.

### 5. Optional follow-on issues

When findings imply engineering work, file follow-ons with
`create-github-issue` and set `depends-on: #<research-issue>`. Do not
implement those follow-ons under this skill.

### 6. Close out

1. Tick acceptance criteria on the issue body when the verifier passes.
2. Open a PR with `Closes #<N>` if the artifact is committed in-repo
   (`create_pr.py --agent <ID> --model-family <FAMILY>`), **or** move the
   issue to Done when findings are comment-only and verification passed.
3. Hand off: `update_issue_status.py --issue <N> --status "In Review"` when a
   PR is opened; Done is reached through the normal merge gate or, for
   comment-only research with no code change, after verification is recorded
   and status is moved to Done by the governed path the board uses for
   docs-only close-out.

## Hard rules

- No research without a board issue.
- No Done without `verify_citations.py` exit 0.
- Never post secrets, diffs, prompts, or raw test logs into findings.
- Citation resolution failures are acceptance failures, not warnings.
