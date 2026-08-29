from __future__ import annotations

from typing import Any

import pytest

import common
import update_issue_status as uis


def _ok_result():
    return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()


def epic_body(*, children: list[int] | None = None, policy: str | None = "children-only", depends_on: list[int] | None = None) -> str:
    lines = ["## Child Issues"] + [f"- #{n}" for n in (children or [91, 92])]
    if policy is not None:
        lines.extend(["", f"epic-close-policy: {policy}"])
    if depends_on:
        lines.extend(f"depends-on: #{n}" for n in depends_on)
    return "\n".join(lines) + "\n"


def epic_record(
    *, number: int = 100, children: list[int] | None = None, policy: str | None = "children-only",
    depends_on: list[int] | None = None, labels: list[str] | None = None, state: str = "OPEN",
) -> dict[str, Any]:
    return {
        "number": number, "state": state,
        "body": epic_body(children=children, policy=policy, depends_on=depends_on),
        "labels": [{"name": name} for name in (labels if labels is not None else ["type:epic", "status:backlog"])],
    }


def child_snapshot(number: int, *, state: str = "CLOSED", status: str = "done", repository: str = "owner/repo") -> dict[str, Any]:
    return {"number": number, "state": state, "repository": repository, "labels": [{"name": f"status:{status}"}]}


_real_status_of = common.status_of
_UNSET = object()


def mock_epic_context(
    monkeypatch: pytest.MonkeyPatch,
    *,
    epic: dict[str, Any] | None = None,
    issue_fn: Any = None,
    snapshots: dict[int, Any] | None = None,
    status_fn: Any = None,
    status: Any = _UNSET,
    project_status_fn: Any = None,
    project_status: Any = _UNSET,
    depends: list[int] | None = None,
) -> None:
    monkeypatch.setattr(uis, "repo_slug", lambda cwd=None: "owner/repo")
    monkeypatch.setattr(common, "repo_slug", lambda cwd=None: "owner/repo")
    monkeypatch.setattr(uis, "unresolved_dependencies", lambda record, cwd=None: depends or [])
    monkeypatch.setattr(common, "unresolved_dependencies", lambda record, cwd=None: depends or [])
    if snapshots is not None:
        monkeypatch.setattr(uis, "child_issue_snapshots", lambda numbers, cwd=None: snapshots)
    if issue_fn is not None or epic is not None:
        fn = issue_fn if issue_fn is not None else (lambda number, cwd=None: epic)
        monkeypatch.setattr(uis, "issue", fn)
        monkeypatch.setattr(common, "issue", fn)
    if status_fn is not None or status is not _UNSET:
        def default_sfn(record):
            if status_fn is not None:
                return status_fn(record)
            st = _real_status_of(record)
            if st is not None:
                return st
            if status is not _UNSET:
                return status
            return "Done"
        monkeypatch.setattr(uis, "status_of", default_sfn)
        monkeypatch.setattr(common, "status_of", default_sfn)
    if project_status_fn is not None or project_status is not _UNSET:
        pfn = project_status_fn if project_status_fn is not None else (lambda number, cwd=None: project_status)
        monkeypatch.setattr(uis, "project_item_status", pfn)
        monkeypatch.setattr(common, "project_item_status", pfn)


def _tripwire_no_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly (not with a caught KernelError) if any mutation is attempted."""
    def _boom(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("blocked epic reconciliation must not mutate")
    for mod in (uis, common):
        for name in ("run", "set_status", "board_edit", "ensure_label"):
            monkeypatch.setattr(mod, name, _boom)


def _mock_mutation_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    commands: list[Any] | None = None,
    board_edit_fn: Any = None,
) -> None:
    run_fn = (lambda argv, **kw: commands.append(argv) or _ok_result()) if commands is not None else (lambda *a, **kw: _ok_result())
    b_edit = board_edit_fn if board_edit_fn is not None else (lambda number, status, *a, **kw: ["project", "item-edit", "--id", "i1", "--single-select-option-id", f"opt_{status.lower()}"])
    for mod in (uis, common):
        monkeypatch.setattr(mod, "run", run_fn)
        monkeypatch.setattr(mod, "ensure_label", lambda *a, **kw: None)
        monkeypatch.setattr(mod, "board_edit", b_edit)
