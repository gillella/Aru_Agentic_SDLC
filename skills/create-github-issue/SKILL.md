---
name: create-github-issue
description: Procedure for creating well-structured, prioritized GitHub issues with dependency tags and project board placement.
---

# Create GitHub Issue Procedure

This skill defines the declarative workflow for creating clear, actionable GitHub issues.

---

## Procedure Steps

### 1. Identify Requirement & Scope
- Determine issue type: `feature`, `bug`, or `task`.
- Define clear summary, background context, and explicit acceptance criteria.

### 2. Specify Dependencies & Parallel Eligibility
- Identify if the issue depends on prior issues being completed (`depends-on: #X`).
- Mark whether the issue can be implemented independently in parallel (`parallel-eligible: true`).

### 3. Format & Submit Issue
- Apply appropriate title prefixes (`feat: `, `fix: `, `chore: `).
- Use structured Markdown issue templates from `.github/ISSUE_TEMPLATE/`.
- Submit issue and assign initial labels and project board status (`Backlog` / `Ready`).
