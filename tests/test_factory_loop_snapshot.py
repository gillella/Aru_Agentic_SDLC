# line-ceiling: 1050
"""Unit tests for factory_loop_snapshot.py (#466).

Validates deterministic, project-agnostic read-only factory loop snapshot
reporting, complete schema coverage, stable ordering, fail-closed handling,
read-only execution guarantee, and hermetic two-repository isolation.
"""

import contextlib
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = str(Path(__file__).resolve().parents[1] / "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import common
import factory_loop_snapshot as fls
import fleet_status as fs
import picker_board_inventory as pbi

MAIN_WORKTREE = "worktree /repo\nHEAD abc1234\nbranch refs/heads/main\n"


def make_issue(
    number: int,
    *,
    title: str = "Issue Title",
    status: str = "Ready",
    priority: str = "p1",
    agent: str = None,
    touches: str = "scripts/common.py",
    depends_on: str = "",
    body: str = "",
):
    labels = [
        {"name": f"status:{status.lower().replace(' ', '-')}"},
        {"name": f"priority:{priority.lower()}"},
        {"name": "type:feat"},
    ]
    if agent:
        labels.append({"name": f"agent:{agent}"})

    doc_body = body or f"Touches: {touches}\n"
    if depends_on:
        doc_body += f"Depends-on: {depends_on}\n"

    return {
        "number": number,
        "title": title,
        "labels": labels,
        "assignees": [],
        "state": "open",
        "user": {"login": "gillella"},
        "body": doc_body,
        "updated_at": "2026-08-26T00:00:00Z",
        "author_association": "OWNER",
    }


def make_pull(
    number: int,
    *,
    title: str = "PR Title",
    branch: str = "feat/issue-1-test",
    head_sha: str = "deadbeef",
    author: str = "agent-1",
    draft: bool = False,
    review_service: str = "review:coderabbit",
    status_check_rollup: list = None,
):
    labels = []
    if author:
        labels.append({"name": f"author:{author}"})
    if review_service:
        labels.append({"name": review_service})

    pr_dict = {
        "number": number,
        "title": title,
        "draft": draft,
        "isDraft": draft,
        "labels": labels,
        "head": {"ref": branch, "sha": head_sha},
        "headRefName": branch,
        "headRefOid": head_sha,
        "updated_at": "2026-08-26T00:00:00Z",
        "updatedAt": "2026-08-26T00:00:00Z",
        "createdAt": "2026-08-26T00:00:00Z",
        "body": "Closes #1",
        "mergeable_state": "clean",
    }
    if status_check_rollup is not None:
        pr_dict["statusCheckRollup"] = status_check_rollup
    return pr_dict


class FakeRepo:
    """Answers git and gh commands for one hermetic repository."""

    def __init__(
        self,
        *,
        slug: str = "gillella/Aru_Agentic_SDLC",
        issues: tuple = (),
        prs: tuple = (),
        worktrees: str = None,
        board_items: list = None,
        projects: list = None,
        tags: list = None,
        framework_version: str = "v0.1.0",
        fail: tuple = (),
    ):
        self.slug = slug
        self.issues = list(issues)
        self.prs = list(prs)
        self.worktrees = MAIN_WORKTREE if worktrees is None else worktrees
        self.fail = set(fail)
        self.tags = ["v0.1.0"] if tags is None else list(tags)
        self.framework_version = framework_version

        owner, _, repo_name = slug.partition("/")
        self.owner = owner
        self.repo_name = repo_name
        self.projects = projects if projects is not None else [{
            "id": "PVT_1",
            "number": 7,
            "title": f"{repo_name} Board",
            "owner": {"login": owner},
            "repositories": {"nodes": [{"nameWithOwner": slug}]},
        }]

        if board_items is not None:
            self.board_items = board_items
        else:
            self.board_items = [
                {
                    "status": (
                        next((lbl["name"].split(":", 1)[1].replace("-", " ").title()
                              for lbl in issue_obj["labels"]
                              if lbl["name"].startswith("status:")), "Ready")
                    ),
                    "content": {"number": issue_obj["number"], "repository": self.slug},
                }
                for issue_obj in self.issues
            ]

        self.commands = []

    def _item_list_json(self):
        return json.dumps({"items": self.board_items, "totalCount": len(self.board_items)})

    def __call__(self, cmd, check=True, cwd=None, **kwargs):
        cmd = list(cmd)
        self.commands.append(cmd)
        joined = " ".join(cmd)

        if "slug" in self.fail and cmd[:2] == ["git", "remote"]:
            return 1, "", "no origin"
        if cmd[:2] == ["git", "remote"]:
            return 0, f"git@github.com:{self.slug}.git\n", ""
        if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return 0, "main\n", ""
        if cmd[:2] == ["git", "rev-parse"]:
            return 0, "abc1234\n", ""
        if cmd[:2] == ["git", "tag"]:
            return 0, "\n".join(self.tags) + "\n", ""
        if cmd[:2] == ["git", "describe"]:
            if cwd and os.path.abspath(str(cwd)) == os.path.abspath(common.get_framework_root()):
                return 0, f"{self.framework_version}\n", ""
            if self.tags:
                return 0, f"{self.tags[-1]}\n", ""
            return 1, "", "No names found"
        if cmd[:3] == ["git", "worktree", "list"]:
            if "worktrees" in self.fail:
                return 1, "", "fatal: not a git repository"
            return 0, self.worktrees, ""
        if "collaborators" in joined:
            if "collaborators" in self.fail:
                return 1, "", "collaborators api down"
            return 0, f"{self.owner}\n", ""
        if "issues?state=open" in joined:
            if "issues" in self.fail:
                return 1, "", "issues api down"
            return 0, "\n".join(json.dumps(row) for row in self.issues), ""
        if cmd[:3] == ["gh", "api", "graphql"]:
            if "projects" in self.fail:
                return 1, "", "graphql api down"
            return 0, json.dumps({"data": {"repository": {"projectsV2": {"nodes": self.projects}}}}), ""
        if cmd[:3] == ["gh", "project", "item-list"]:
            if "board" in self.fail:
                return 1, "", "board item api down"
            return 0, self._item_list_json(), ""
        if cmd[:3] == ["gh", "pr", "list"]:
            if "prs" in self.fail or "graphql" in self.fail or "rich_prs" in self.fail:
                return 1, "", "pr list failed"
            return 0, json.dumps(self.prs), ""
        if "pulls?state=open" in joined:
            if "prs" in self.fail:
                return 1, "", "prs api down"
            return 0, "\n".join(json.dumps(row) for row in self.prs), ""

        raise AssertionError(f"Unexpected command executed in test: {cmd}")


@contextlib.contextmanager
def wired_repo(repo: FakeRepo):
    """Route all execution through FakeRepo."""
    with patch.object(common, "run_cmd", repo), \
         patch.object(pbi, "run_cmd", repo), \
         patch.object(fs, "run_cmd", repo), \
         patch.object(fls, "run_cmd", repo), \
         patch("os.chdir"), patch("os.path.isdir", return_value=True):
        yield repo


class TestSnapshotSchemaAndFields(unittest.TestCase):
    def test_complete_snapshot_has_all_versioned_fields(self):
        issue1 = make_issue(101, title="Feature A", status="Ready", priority="p1", touches="scripts/a.py")
        issue2 = make_issue(102, title="Feature B", status="In Progress", priority="p0", agent="agent-1", touches="scripts/b.py", depends_on="#101")
        pr1 = make_pull(
            201,
            title="PR for Feature A",
            branch="feat/issue-101-a",
            author="agent-2",
            status_check_rollup=[{"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"}],
        )

        worktrees = (
            "worktree /repo\nHEAD abc1234\nbranch refs/heads/main\n\n"
            "worktree /repo/.worktrees/feat-issue-102-b__agent-1\nHEAD def5678\nbranch refs/heads/feat/issue-102-b\n"
        )

        repo = FakeRepo(issues=[issue1, issue2], prs=[pr1], worktrees=worktrees)
        with wired_repo(repo):
            snapshot = fls.evaluate_factory_loop_snapshot(".")

        self.assertEqual(snapshot["schema_version"], "aru.factory_loop_snapshot.v1")
        self.assertFalse(snapshot["degraded"])
        self.assertEqual(snapshot["errors"], [])
        self.assertEqual(snapshot["state"], "waiting")
        self.assertEqual(snapshot["exit_code"], fls.EXIT_WAITING)

        # Repository identity
        self.assertIsNotNone(snapshot["repository"])
        self.assertEqual(snapshot["repository"]["slug"], "gillella/Aru_Agentic_SDLC")
        self.assertEqual(snapshot["repository"]["owner"], "gillella")
        self.assertEqual(snapshot["repository"]["name"], "Aru_Agentic_SDLC")
        self.assertEqual(snapshot["repository"]["current_branch"], "main")
        self.assertEqual(snapshot["repository"]["head_sha"], "abc1234")

        # Board identity and counts
        self.assertIsNotNone(snapshot["board"])
        self.assertEqual(snapshot["board"]["number"], 7)
        self.assertEqual(snapshot["board"]["owner"], "gillella")
        self.assertIn("Ready", snapshot["board"]["status_counts"])
        self.assertIn("In Progress", snapshot["board"]["status_counts"])

        # Issues
        self.assertEqual(len(snapshot["open_issues"]), 2)
        i1 = snapshot["open_issues"][0]
        self.assertEqual(i1["number"], 101)
        self.assertEqual(i1["board_status"], "Ready")
        self.assertEqual(i1["priority"], "P1")
        self.assertEqual(i1["touches"], ["scripts/a.py"])

        # PRs
        self.assertEqual(len(snapshot["open_pull_requests"]), 1)
        p1 = snapshot["open_pull_requests"][0]
        self.assertEqual(p1["number"], 201)
        self.assertEqual(p1["author"], "agent-2")
        self.assertEqual(p1["review_authority"]["assigned"], "coderabbit")
        self.assertEqual(p1["ci"]["state"], "PASSED")
        self.assertEqual(p1["ci"]["summary"], "All CI status checks passed.")
        self.assertTrue(p1["merge_gate"]["ready"])
        self.assertEqual(p1["merge_gate"]["blockers"], [])

        # Claims
        self.assertIn({"type": "issue", "number": 102, "agent": "agent-1"}, snapshot["claims"])
        self.assertIn({"type": "pr", "number": 201, "agent": "agent-2"}, snapshot["claims"])

        # Dependencies
        self.assertEqual(len(snapshot["dependencies"]), 1)
        self.assertEqual(snapshot["dependencies"][0]["issue"], 102)
        self.assertEqual(snapshot["dependencies"][0]["depends_on"], [101])
        self.assertEqual(snapshot["dependencies"][0]["unresolved"], [101])

        # Touches reservations
        self.assertEqual(len(snapshot["touches_reservations"]), 1)
        self.assertEqual(snapshot["touches_reservations"][0]["issue"], 102)
        self.assertEqual(snapshot["touches_reservations"][0]["paths"], ["scripts/b.py"])

        # Worktrees
        self.assertEqual(len(snapshot["worktrees"]), 2)
        wt_agent = next(wt for wt in snapshot["worktrees"] if wt["issue"] == 102)
        self.assertEqual(wt_agent["agent"], "agent-1")
        self.assertFalse(wt_agent["stale"])

        # Worker assignments
        self.assertIn("agent-1", snapshot["worker_assignments"])
        self.assertEqual(snapshot["worker_assignments"]["agent-1"]["issues"], [102])
        self.assertIn("agent-2", snapshot["worker_assignments"])
        self.assertEqual(snapshot["worker_assignments"]["agent-2"]["prs"], [201])

        # Tags & releases
        self.assertEqual(snapshot["tags_releases"]["latest_tag"], "v0.1.0")

        # Claimable work (issue 101 is ready and unblocked)
        self.assertEqual(len(snapshot["claimable_work"]), 1)
        self.assertEqual(snapshot["claimable_work"][0]["issue"], 101)
        self.assertEqual(snapshot["claimable_work"][0]["priority"], "P1")


class TestDeterministicOutput(unittest.TestCase):
    def test_output_ordering_and_values_are_strictly_deterministic(self):
        issue1 = make_issue(300, title="Z issue", status="Ready", priority="p2")
        issue2 = make_issue(100, title="A issue", status="Ready", priority="p0")
        pr1 = make_pull(50, author="agent-z")
        pr2 = make_pull(10, author="agent-a")

        repo = FakeRepo(issues=[issue1, issue2], prs=[pr1, pr2])
        with wired_repo(repo):
            snap1 = fls.evaluate_factory_loop_snapshot(".")
            snap2 = fls.evaluate_factory_loop_snapshot(".")

        json1 = json.dumps(snap1, sort_keys=True)
        json2 = json.dumps(snap2, sort_keys=True)
        self.assertEqual(json1, json2)

        # Verify sorted lists
        issue_nums = [i["number"] for i in snap1["open_issues"]]
        self.assertEqual(issue_nums, [100, 300])

        pr_nums = [p["number"] for p in snap1["open_pull_requests"]]
        self.assertEqual(pr_nums, [10, 50])

        # Verify priority ordering for claimable work (P0 before P2)
        claimable_nums = [c["issue"] for c in snap1["claimable_work"]]
        self.assertEqual(claimable_nums, [100, 300])

    def test_no_secret_or_unredacted_paths(self):
        repo = FakeRepo(worktrees="worktree /private/var/folders/xyz/repo\nbranch refs/heads/main\n")
        with wired_repo(repo):
            snapshot = fls.evaluate_factory_loop_snapshot("/private/var/folders/xyz/repo")

        json_text = json.dumps(snapshot)
        self.assertNotIn("/private/var/folders/xyz/repo", json_text)
        self.assertIn("<local-path>", json_text)


class TestFailClosedAndDegraded(unittest.TestCase):
    def test_unresolvable_repo_fails_closed(self):
        repo = FakeRepo(fail={"slug"})
        with wired_repo(repo):
            snapshot = fls.evaluate_factory_loop_snapshot(".")

        self.assertTrue(snapshot["degraded"])
        self.assertEqual(snapshot["state"], "error")
        self.assertEqual(snapshot["exit_code"], fls.EXIT_ERROR)
        self.assertTrue(len(snapshot["errors"]) > 0)
        self.assertIsNone(snapshot["repository"])
        self.assertIsNone(snapshot["tags_releases"]["framework_version"])

    def test_unreadable_board_is_blocked_and_degraded(self):
        repo = FakeRepo(issues=[make_issue(1)], fail={"board"})
        with wired_repo(repo):
            snapshot = fls.evaluate_factory_loop_snapshot(".")

        self.assertTrue(snapshot["degraded"])
        self.assertEqual(snapshot["state"], "blocked")
        self.assertEqual(snapshot["exit_code"], fls.EXIT_BLOCKED)
        self.assertTrue(any("Could not read items for board" in e for e in snapshot["errors"]))
        self.assertIsNone(snapshot["tags_releases"]["framework_version"])

    def test_ambiguous_board_fails_closed(self):
        second = {
            "id": "PVT_2",
            "number": 8,
            "title": "Aru_Agentic_SDLC Board",
            "owner": {"login": "gillella"},
            "repositories": {"nodes": [{"nameWithOwner": "gillella/Aru_Agentic_SDLC"}]},
        }
        repo = FakeRepo(issues=[make_issue(1)])
        repo.projects.append(second)
        with wired_repo(repo):
            snapshot = fls.evaluate_factory_loop_snapshot(".")

        self.assertTrue(snapshot["degraded"])
        self.assertEqual(snapshot["state"], "blocked")
        self.assertEqual(snapshot["exit_code"], fls.EXIT_BLOCKED)

    def test_malformed_board_item_content_fails_closed(self):
        repo = FakeRepo(
            issues=[make_issue(1)],
            board_items=[{"status": "Ready", "content": "not-a-dict"}],
        )
        with wired_repo(repo):
            snapshot = fls.evaluate_factory_loop_snapshot(".")

        self.assertTrue(snapshot["degraded"])
        self.assertEqual(snapshot["state"], "blocked")
        self.assertEqual(snapshot["exit_code"], fls.EXIT_BLOCKED)
        self.assertTrue(any("Malformed item content" in e for e in snapshot["errors"]))

    def test_malformed_board_item_fails_closed(self):
        repo = FakeRepo(
            issues=[make_issue(1)],
            board_items=["not-a-dict"],
        )
        with wired_repo(repo):
            snapshot = fls.evaluate_factory_loop_snapshot(".")

        self.assertTrue(snapshot["degraded"])
        self.assertEqual(snapshot["state"], "blocked")
        self.assertEqual(snapshot["exit_code"], fls.EXIT_BLOCKED)
        self.assertTrue(any("Malformed item on board" in e for e in snapshot["errors"]))

    def test_untrusted_metadata_author_excluded_from_claimable(self):
        spoofed = make_issue(30, status="Ready")
        spoofed["user"] = {"login": "outsider"}
        spoofed["author_association"] = "NONE"
        repo = FakeRepo(issues=[spoofed])
        with wired_repo(repo):
            snapshot = fls.evaluate_factory_loop_snapshot(".")

        self.assertNotIn(30, [c["issue"] for c in snapshot["claimable_work"]])

    def test_api_issue_failure_fails_closed(self):
        repo = FakeRepo(fail={"issues"})
        with wired_repo(repo):
            snapshot = fls.evaluate_factory_loop_snapshot(".")

        self.assertTrue(snapshot["degraded"])
        self.assertEqual(snapshot["state"], "error")
        self.assertEqual(snapshot["exit_code"], fls.EXIT_ERROR)

    def test_api_pull_failure_fails_closed(self):
        repo = FakeRepo(fail={"prs"})
        with wired_repo(repo):
            snapshot = fls.evaluate_factory_loop_snapshot(".")

        self.assertTrue(snapshot["degraded"])
        self.assertEqual(snapshot["state"], "error")
        self.assertEqual(snapshot["exit_code"], fls.EXIT_ERROR)

    def test_unreadable_worktree_fails_closed(self):
        repo = FakeRepo(fail={"worktrees"})
        with wired_repo(repo):
            snapshot = fls.evaluate_factory_loop_snapshot(".")

        self.assertTrue(snapshot["degraded"])
        self.assertEqual(snapshot["state"], "error")
        self.assertEqual(snapshot["exit_code"], fls.EXIT_ERROR)

    def test_open_issue_missing_from_board_fails_closed(self):
        repo = FakeRepo(
            issues=[make_issue(101), make_issue(102)],
            board_items=[{"status": "Ready", "content": {"number": 101, "repository": "gillella/Aru_Agentic_SDLC"}}],
        )
        with wired_repo(repo):
            snapshot = fls.evaluate_factory_loop_snapshot(".")

        self.assertTrue(snapshot["degraded"])
        self.assertEqual(snapshot["state"], "blocked")
        self.assertEqual(snapshot["exit_code"], fls.EXIT_BLOCKED)
        self.assertTrue(any("missing from canonical Project v2 board" in e for e in snapshot["errors"]))

    def test_duplicate_board_item_for_issue_fails_closed(self):
        repo = FakeRepo(
            issues=[make_issue(101)],
            board_items=[
                {"status": "Ready", "content": {"number": 101, "repository": "gillella/Aru_Agentic_SDLC"}},
                {"status": "In Progress", "content": {"number": 101, "repository": "gillella/Aru_Agentic_SDLC"}},
            ],
        )
        with wired_repo(repo):
            snapshot = fls.evaluate_factory_loop_snapshot(".")

        self.assertTrue(snapshot["degraded"])
        self.assertEqual(snapshot["state"], "blocked")
        self.assertEqual(snapshot["exit_code"], fls.EXIT_BLOCKED)
        self.assertTrue(any("Duplicate board item" in e for e in snapshot["errors"]))

    def test_empty_status_board_item_fails_closed(self):
        repo = FakeRepo(
            issues=[make_issue(101)],
            board_items=[{"status": "", "content": {"number": 101, "repository": "gillella/Aru_Agentic_SDLC"}}],
        )
        with wired_repo(repo):
            snapshot = fls.evaluate_factory_loop_snapshot(".")

        self.assertTrue(snapshot["degraded"])
        self.assertEqual(snapshot["state"], "blocked")
        self.assertEqual(snapshot["exit_code"], fls.EXIT_BLOCKED)
        self.assertTrue(any("empty or missing status" in e for e in snapshot["errors"]))

    def test_whitespace_status_board_item_fails_closed(self):
        repo = FakeRepo(
            issues=[make_issue(101)],
            board_items=[{"status": "   ", "content": {"number": 101, "repository": "gillella/Aru_Agentic_SDLC"}}],
        )
        with wired_repo(repo):
            snapshot = fls.evaluate_factory_loop_snapshot(".")

        self.assertTrue(snapshot["degraded"])
        self.assertEqual(snapshot["state"], "blocked")
        self.assertEqual(snapshot["exit_code"], fls.EXIT_BLOCKED)
        self.assertTrue(any("empty or missing status" in e for e in snapshot["errors"]))


class TestReadOnlyGuarantee(unittest.TestCase):
    def test_executes_no_mutating_commands(self):
        issue1 = make_issue(10, status="Ready")
        pr1 = make_pull(20, author="agent-1")
        repo = FakeRepo(issues=[issue1], prs=[pr1])

        with wired_repo(repo):
            fls.evaluate_factory_loop_snapshot(".")

        mutating = {"edit", "create", "merge", "close", "comment", "delete", "item-edit", "checkout", "push"}
        for cmd in repo.commands:
            intersect = set(cmd) & mutating
            self.assertFalse(intersect, f"Mutating command invoked: {cmd}")


class TestTwoRepositoryHermeticIsolation(unittest.TestCase):
    def test_resolves_two_different_consumer_boards_independently(self):
        repo_a = FakeRepo(
            slug="org-alpha/project-alpha",
            issues=[make_issue(1, title="Alpha 1")],
            tags=["v-alpha-1.0.0"],
            projects=[{"id": "PVT_A", "number": 10, "title": "project-alpha Board",
                       "owner": {"login": "org-alpha"},
                       "repositories": {"nodes": [{"nameWithOwner": "org-alpha/project-alpha"}]}}],
        )

        repo_b = FakeRepo(
            slug="org-beta/project-beta",
            issues=[make_issue(2, title="Beta 2")],
            tags=["v-beta-2.0.0"],
            projects=[{"id": "PVT_B", "number": 20, "title": "project-beta Board",
                       "owner": {"login": "org-beta"},
                       "repositories": {"nodes": [{"nameWithOwner": "org-beta/project-beta"}]}}],
        )

        with wired_repo(repo_a):
            snap_a = fls.evaluate_factory_loop_snapshot("/path/to/alpha")

        with wired_repo(repo_b):
            snap_b = fls.evaluate_factory_loop_snapshot("/path/to/beta")

        # Verify Repo A
        self.assertEqual(snap_a["repository"]["slug"], "org-alpha/project-alpha")
        self.assertEqual(snap_a["board"]["number"], 10)
        self.assertEqual(snap_a["board"]["title"], "project-alpha Board")
        self.assertEqual(snap_a["open_issues"][0]["title"], "Alpha 1")
        self.assertEqual(snap_a["tags_releases"]["latest_tag"], "v-alpha-1.0.0")
        self.assertEqual(snap_a["tags_releases"]["framework_version"], "v0.1.0")

        # Verify Repo B
        self.assertEqual(snap_b["repository"]["slug"], "org-beta/project-beta")
        self.assertEqual(snap_b["board"]["number"], 20)
        self.assertEqual(snap_b["board"]["title"], "project-beta Board")
        self.assertEqual(snap_b["open_issues"][0]["title"], "Beta 2")
        self.assertEqual(snap_b["tags_releases"]["latest_tag"], "v-beta-2.0.0")
        self.assertEqual(snap_b["tags_releases"]["framework_version"], "v0.1.0")

        # Verify zero cross-talk
        self.assertNotEqual(snap_a["repository"]["slug"], snap_b["repository"]["slug"])
        self.assertNotEqual(snap_a["board"]["id"], snap_b["board"]["id"])


class TestCLIAndFormatting(unittest.TestCase):
    def test_text_formatting(self):
        issue1 = make_issue(10, title="Implement Feature", status="Ready", priority="p0")
        repo = FakeRepo(issues=[issue1])
        with wired_repo(repo):
            snapshot = fls.evaluate_factory_loop_snapshot(".")
            text = fls.format_snapshot_text(snapshot)

        self.assertIn("=== Aru_Agentic_SDLC: Factory Loop Snapshot ===", text)
        self.assertIn("State: WAITING", text)
        self.assertIn("Repository: gillella/Aru_Agentic_SDLC", text)
        self.assertIn("Board: #7", text)
        self.assertIn("#10 [P0] Implement Feature", text)

    def test_text_formatting_with_consumer_repository(self):
        issue1 = make_issue(10, title="Consumer Feature", status="Ready", priority="p0")
        repo = FakeRepo(slug="org-beta/project-beta", issues=[issue1])
        with wired_repo(repo):
            snapshot = fls.evaluate_factory_loop_snapshot("/path/to/beta")
            text = fls.format_snapshot_text(snapshot)

        self.assertIn("=== project-beta: Factory Loop Snapshot ===", text)
        self.assertNotIn("=== Aru_Agentic_SDLC: Factory Loop Snapshot ===", text)
        self.assertIn("Repository: org-beta/project-beta", text)

    def test_cli_json_flag(self):
        repo = FakeRepo(issues=[make_issue(1)])
        with wired_repo(repo), patch("sys.argv", ["factory_loop_snapshot.py", "--json"]), \
             patch("builtins.print") as mock_print:
            with self.assertRaises(SystemExit) as raised:
                fls.main()

            self.assertEqual(raised.exception.code, fls.EXIT_WAITING)
            output = mock_print.call_args.args[0]
            parsed = json.loads(output)
            self.assertEqual(parsed["schema_version"], "aru.factory_loop_snapshot.v1")


class TestLifecycleTransitionsAndFilters(unittest.TestCase):
    def test_empty_repository_is_complete(self):
        repo = FakeRepo(issues=[], prs=[])
        with wired_repo(repo):
            snapshot = fls.evaluate_factory_loop_snapshot(".")

        self.assertEqual(snapshot["state"], "complete")
        self.assertEqual(snapshot["exit_code"], fls.EXIT_COMPLETE)
        self.assertIn("COMPLETE:", snapshot["summary"])
        self.assertEqual(len(snapshot["open_issues"]), 0)
        self.assertEqual(len(snapshot["open_pull_requests"]), 0)
        self.assertEqual(len(snapshot["claims"]), 0)
        self.assertEqual(len(snapshot["claimable_work"]), 0)

    def test_drifted_issue_detected(self):
        issue = make_issue(401, status="Ready")
        # Board says In Progress while label says status:ready
        repo = FakeRepo(
            issues=[issue],
            board_items=[{"status": "In Progress", "content": {"number": 401, "repository": "gillella/Aru_Agentic_SDLC"}}],
        )
        with wired_repo(repo):
            snapshot = fls.evaluate_factory_loop_snapshot(".")

        self.assertEqual(len(snapshot["open_issues"]), 1)
        self.assertTrue(snapshot["open_issues"][0]["drifted"])
        self.assertEqual(snapshot["open_issues"][0]["board_status"], "In Progress")
        self.assertEqual(snapshot["open_issues"][0]["label_status"], "ready")

    def test_stale_worktree_detected(self):
        worktrees = (
            "worktree /repo\nHEAD abc1234\nbranch refs/heads/main\n\n"
            "worktree /repo/.worktrees/feat-issue-999-old__agent-1\nHEAD def5678\nbranch refs/heads/feat/issue-999-old\n"
        )
        # Issue 999 is closed / not in open issues
        repo = FakeRepo(issues=[make_issue(101)], worktrees=worktrees)
        with wired_repo(repo):
            snapshot = fls.evaluate_factory_loop_snapshot(".")

        self.assertEqual(len(snapshot["worktrees"]), 2)
        stale_wt = next(wt for wt in snapshot["worktrees"] if wt["issue"] == 999)
        self.assertTrue(stale_wt["stale"])
        self.assertEqual(stale_wt["agent"], "agent-1")

    def test_blocked_by_dependency_excluded_from_claimable(self):
        issue_open = make_issue(10, status="In Progress", agent="agent-1")
        issue_blocked = make_issue(20, status="Ready", depends_on="#10")
        repo = FakeRepo(issues=[issue_open, issue_blocked])
        with wired_repo(repo):
            snapshot = fls.evaluate_factory_loop_snapshot(".")

        # Issue 20 should be in dependencies as unresolved, not claimable
        self.assertEqual(len(snapshot["dependencies"]), 1)
        self.assertEqual(snapshot["dependencies"][0]["issue"], 20)
        self.assertEqual(snapshot["dependencies"][0]["unresolved"], [10])

        claimable_issues = [c["issue"] for c in snapshot["claimable_work"]]
        self.assertNotIn(20, claimable_issues)

    def test_touches_conflict_excluded_from_claimable(self):
        in_flight = make_issue(10, status="In Progress", agent="agent-1", touches="scripts/common.py")
        conflicting = make_issue(20, status="Ready", touches="scripts/common.py")
        repo = FakeRepo(issues=[in_flight, conflicting])
        with wired_repo(repo):
            snapshot = fls.evaluate_factory_loop_snapshot(".")

        claimable_issues = [c["issue"] for c in snapshot["claimable_work"]]
        self.assertNotIn(20, claimable_issues)

    def test_epic_and_needs_human_excluded_from_claimable(self):
        epic = make_issue(10, title="Epic container", status="Ready")
        epic["labels"].append({"name": "type:epic"})

        operator = make_issue(20, title="Operator task", status="Ready")
        operator["labels"].append({"name": "needs-human"})

        repo = FakeRepo(issues=[epic, operator])
        with wired_repo(repo):
            snapshot = fls.evaluate_factory_loop_snapshot(".")

        self.assertEqual(len(snapshot["open_issues"]), 2)
        claimable_issues = [c["issue"] for c in snapshot["claimable_work"]]
        self.assertEqual(claimable_issues, [])

    def test_extract_ci_summary_states(self):
        pr_fail = {"statusCheckRollup": [{"name": "test", "status": "COMPLETED", "conclusion": "FAILURE"}]}
        pr_pending = {"statusCheckRollup": [{"name": "test", "status": "IN_PROGRESS", "conclusion": None}]}
        pr_success = {"statusCheckRollup": [{"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"}]}
        pr_empty = {"statusCheckRollup": []}
        pr_none = {}

        self.assertEqual(fls._extract_ci_summary(pr_fail)["state"], "FAILED")
        self.assertEqual(fls._extract_ci_summary(pr_pending)["state"], "PENDING")
        self.assertEqual(fls._extract_ci_summary(pr_success)["state"], "PASSED")
        self.assertEqual(fls._extract_ci_summary(pr_empty)["state"], "NONE")
        self.assertEqual(fls._extract_ci_summary(pr_none)["state"], "UNAVAILABLE")

    def test_pr_rest_ci_state_reports_unavailable_and_blocks_merge_honestly(self):
        pr1 = make_pull(1, title="PR 1", status_check_rollup=None)
        repo = FakeRepo(prs=[pr1], fail={"graphql"})
        with wired_repo(repo):
            snapshot = fls.evaluate_factory_loop_snapshot(".")

        p1 = next(p for p in snapshot["open_pull_requests"] if p["number"] == 1)
        self.assertEqual(p1["ci"]["state"], "UNAVAILABLE")
        self.assertFalse(p1["merge_gate"]["ready"])
        self.assertIn("CI check status is unavailable.", p1["merge_gate"]["blockers"])

    def test_pr_with_passing_ci_checks_is_merge_ready(self):
        pr1 = make_pull(
            1,
            author="agent-1",
            review_service="review:coderabbit",
            status_check_rollup=[{"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"}],
        )
        repo = FakeRepo(prs=[pr1])
        with wired_repo(repo):
            snapshot = fls.evaluate_factory_loop_snapshot(".")

        p1 = snapshot["open_pull_requests"][0]
        self.assertEqual(p1["ci"]["state"], "PASSED")
        self.assertTrue(p1["merge_gate"]["ready"])
        self.assertEqual(p1["merge_gate"]["blockers"], [])

    def test_pr_with_failing_ci_checks_blocks_merge_gate(self):
        pr1 = make_pull(
            1,
            author="agent-1",
            review_service="review:coderabbit",
            status_check_rollup=[{"name": "test", "status": "COMPLETED", "conclusion": "FAILURE"}],
        )
        repo = FakeRepo(prs=[pr1])
        with wired_repo(repo):
            snapshot = fls.evaluate_factory_loop_snapshot(".")

        p1 = snapshot["open_pull_requests"][0]
        self.assertEqual(p1["ci"]["state"], "FAILED")
        self.assertFalse(p1["merge_gate"]["ready"])
        self.assertIn("CI is FAILED.", p1["merge_gate"]["blockers"])

    def test_pr_with_pending_ci_checks_blocks_merge_gate(self):
        pr1 = make_pull(
            1,
            author="agent-1",
            review_service="review:coderabbit",
            status_check_rollup=[{"name": "test", "status": "IN_PROGRESS", "conclusion": None}],
        )
        repo = FakeRepo(prs=[pr1])
        with wired_repo(repo):
            snapshot = fls.evaluate_factory_loop_snapshot(".")

        p1 = snapshot["open_pull_requests"][0]
        self.assertEqual(p1["ci"]["state"], "PENDING")
        self.assertFalse(p1["merge_gate"]["ready"])
        self.assertIn("CI is PENDING.", p1["merge_gate"]["blockers"])

    def test_pr_with_empty_ci_checks_blocks_merge_gate(self):
        pr1 = make_pull(
            1,
            author="agent-1",
            review_service="review:coderabbit",
            status_check_rollup=[],
        )
        repo = FakeRepo(prs=[pr1])
        with wired_repo(repo):
            snapshot = fls.evaluate_factory_loop_snapshot(".")

        p1 = snapshot["open_pull_requests"][0]
        self.assertEqual(p1["ci"]["state"], "NONE")
        self.assertFalse(p1["merge_gate"]["ready"])
        self.assertIn("No CI checks reported on head commit.", p1["merge_gate"]["blockers"])

    def test_touches_reservations_includes_label_status_in_progress_and_in_review(self):
        issue_ip = make_issue(10, status="In Progress", touches="scripts/a.py")
        issue_ip["labels"] = [{"name": "status:in-progress"}, {"name": "type:feat"}]

        issue_ir = make_issue(11, status="In Review", touches="scripts/b.py")
        issue_ir["labels"] = [{"name": "status:in-review"}, {"name": "type:feat"}]

        repo = FakeRepo(issues=[issue_ip, issue_ir])
        with wired_repo(repo):
            snapshot = fls.evaluate_factory_loop_snapshot(".")

        res = {r["issue"]: r["paths"] for r in snapshot["touches_reservations"]}
        self.assertIn(10, res)
        self.assertEqual(res[10], ["scripts/a.py"])
        self.assertIn(11, res)
        self.assertEqual(res[11], ["scripts/b.py"])

    def test_pr_review_authority_varieties(self):
        pr_sourcery = make_pull(1, review_service="review:sourcery")
        pr_codeant = make_pull(2, review_service="review:codeant")
        pr_agent = make_pull(3, review_service="review:agent")
        pr_none = make_pull(4, review_service=None)

        repo = FakeRepo(prs=[pr_sourcery, pr_codeant, pr_agent, pr_none])
        with wired_repo(repo):
            snapshot = fls.evaluate_factory_loop_snapshot(".")

        prs = {p["number"]: p for p in snapshot["open_pull_requests"]}
        self.assertEqual(prs[1]["review_authority"]["assigned"], "sourcery")
        self.assertEqual(prs[2]["review_authority"]["assigned"], "codeant")
        self.assertEqual(prs[3]["review_authority"]["assigned"], "agent")
        self.assertIsNone(prs[4]["review_authority"]["assigned"])

    def test_pr_multiple_review_labels_is_ambiguous_and_blocks_merge(self):
        pr_multi1 = make_pull(1, review_service="review:coderabbit")
        pr_multi1["labels"].append({"name": "review:agent"})

        pr_multi2 = make_pull(2, review_service="review:sourcery")
        pr_multi2["labels"].append({"name": "review:codeant"})

        repo = FakeRepo(prs=[pr_multi1, pr_multi2])
        with wired_repo(repo):
            snapshot = fls.evaluate_factory_loop_snapshot(".")

        prs = {p["number"]: p for p in snapshot["open_pull_requests"]}
        self.assertIsNone(prs[1]["review_authority"]["assigned"])
        self.assertEqual(prs[1]["review_authority"]["state"], "AMBIGUOUS")
        self.assertIn("review:agent", prs[1]["review_authority"]["evidence"])
        self.assertIn("review:coderabbit", prs[1]["review_authority"]["evidence"])
        self.assertFalse(prs[1]["merge_gate"]["ready"])
        self.assertTrue(any("Ambiguous review authority" in b for b in prs[1]["merge_gate"]["blockers"]))

        self.assertIsNone(prs[2]["review_authority"]["assigned"])
        self.assertEqual(prs[2]["review_authority"]["state"], "AMBIGUOUS")
        self.assertFalse(prs[2]["merge_gate"]["ready"])

    def test_candidate_diagnostics_preserves_blocked_conflicted_and_missing_touches(self):
        issue_ready = make_issue(10, status="Ready", touches="scripts/a.py")
        issue_blocked = make_issue(11, status="Ready", touches="scripts/b.py", depends_on="#10")
        issue_inflight = make_issue(12, status="In Progress", agent="agent-x", touches="scripts/c.py")
        issue_conflicted = make_issue(13, status="Ready", touches="scripts/c.py")
        issue_missing = make_issue(14, status="Ready", touches="")

        repo = FakeRepo(issues=[issue_ready, issue_blocked, issue_inflight, issue_conflicted, issue_missing])
        with wired_repo(repo):
            snapshot = fls.evaluate_factory_loop_snapshot(".")

        self.assertFalse(snapshot["degraded"])
        self.assertEqual([c["issue"] for c in snapshot["claimable_work"]], [10])

        diag = snapshot["candidate_diagnostics"]
        self.assertEqual(diag["integrity_issues"], [])
        self.assertEqual(diag["blocked"], [{"number": 11, "blocked_by": [10]}])
        self.assertEqual(diag["conflicted"], [{"number": 13, "conflict": ["scripts/c.py"]}])
        self.assertIn(14, diag["missing_touches"])

    def test_candidate_integrity_failure_duplicate_priority_fails_closed(self):
        issue_dup = make_issue(20, status="Ready")
        issue_dup["labels"] = [
            {"name": "status:ready"},
            {"name": "priority:p1"},
            {"name": "priority:p1"},
            {"name": "type:feat"},
        ]

        repo = FakeRepo(issues=[issue_dup])
        with wired_repo(repo):
            snapshot = fls.evaluate_factory_loop_snapshot(".")

        self.assertTrue(snapshot["degraded"])
        self.assertEqual(snapshot["state"], "blocked")
        self.assertEqual(snapshot["exit_code"], fls.EXIT_BLOCKED)
        self.assertEqual(snapshot["claimable_work"], [])
        self.assertEqual(len(snapshot["candidate_diagnostics"]["integrity_issues"]), 1)
        self.assertEqual(snapshot["candidate_diagnostics"]["integrity_issues"][0]["number"], 20)
        self.assertTrue(any("Candidate evaluation integrity failure on issue #20" in e for e in snapshot["errors"]))

    def test_candidate_integrity_failure_contradictory_priority_fails_closed(self):
        issue_contra = make_issue(21, status="Ready")
        issue_contra["labels"] = [
            {"name": "status:ready"},
            {"name": "priority:p0"},
            {"name": "priority:p2"},
            {"name": "type:feat"},
        ]

        repo = FakeRepo(issues=[issue_contra])
        with wired_repo(repo):
            snapshot = fls.evaluate_factory_loop_snapshot(".")

        self.assertTrue(snapshot["degraded"])
        self.assertEqual(snapshot["state"], "blocked")
        self.assertEqual(snapshot["exit_code"], fls.EXIT_BLOCKED)
        self.assertEqual(snapshot["claimable_work"], [])
        self.assertEqual(len(snapshot["candidate_diagnostics"]["integrity_issues"]), 1)
        self.assertEqual(snapshot["candidate_diagnostics"]["integrity_issues"][0]["number"], 21)

    def test_candidate_evaluation_exception_fails_closed(self):
        repo = FakeRepo(issues=[make_issue(1, status="Ready")])
        with wired_repo(repo), patch("factory_loop_snapshot.build_candidates", side_effect=RuntimeError("eval boom")):
            snapshot = fls.evaluate_factory_loop_snapshot(".")

        self.assertTrue(snapshot["degraded"])
        self.assertEqual(snapshot["state"], "blocked")
        self.assertEqual(snapshot["exit_code"], fls.EXIT_BLOCKED)
        self.assertTrue(any("Candidate evaluation failed: eval boom" in e for e in snapshot["errors"]))

    def test_candidate_evaluation_malformed_result_fails_closed(self):
        repo = FakeRepo(issues=[make_issue(1, status="Ready")])
        with wired_repo(repo), patch("factory_loop_snapshot.build_candidates", return_value="not-a-dict"):
            snapshot = fls.evaluate_factory_loop_snapshot(".")

        self.assertTrue(snapshot["degraded"])
        self.assertEqual(snapshot["state"], "blocked")
        self.assertEqual(snapshot["exit_code"], fls.EXIT_BLOCKED)
        self.assertTrue(any("Candidate evaluation returned malformed result" in e for e in snapshot["errors"]))

    def test_format_snapshot_text_displays_candidate_diagnostics(self):
        mock_snapshot = {
            "schema_version": fls.SCHEMA_VERSION,
            "state": "blocked",
            "summary": "BLOCKED test",
            "repository": {"name": "test-repo", "slug": "org/test-repo", "current_branch": "main"},
            "board": {"number": 1, "title": "Board", "status_counts": {"Ready": 2}},
            "claimable_work": [{"issue": 10, "priority": "P0", "title": "Good issue", "skill": "implement-next-issue"}],
            "candidate_diagnostics": {
                "integrity_issues": [{"number": 20, "reason": "duplicate priority"}],
                "blocked": [{"number": 11, "blocked_by": [10]}],
                "conflicted": [{"number": 12, "conflict": ["scripts/foo.py"]}],
                "missing_touches": [13],
            },
            "worktrees": [],
            "errors": ["Candidate evaluation integrity failure on issue #20: duplicate priority."],
        }
        text = fls.format_snapshot_text(mock_snapshot)
        self.assertIn("Candidate Integrity Issues (1):", text)
        self.assertIn("#20: duplicate priority", text)
        self.assertIn("Blocked Candidates (1):", text)
        self.assertIn("#11 (blocked by #10)", text)
        self.assertIn("Conflicted Candidates (1):", text)
        self.assertIn("#12 (conflict: scripts/foo.py)", text)
        self.assertIn("Missing Touches / Untrusted (1):", text)
        self.assertIn("#13", text)

    def test_collaborator_lookup_failure_propagates_degraded_blocked_candidate_evaluation(self):
        issue1 = make_issue(10, status="Ready")
        repo = FakeRepo(issues=[issue1], fail={"collaborators"})
        with wired_repo(repo):
            snapshot = fls.evaluate_factory_loop_snapshot(".")

        self.assertTrue(snapshot["degraded"])
        self.assertEqual(snapshot["state"], "blocked")
        self.assertEqual(snapshot["exit_code"], fls.EXIT_BLOCKED)
        self.assertEqual(snapshot["claimable_work"], [])
        self.assertTrue(any("Could not resolve trusted collaborator logins" in e for e in snapshot["errors"]))

    def test_collaborator_lookup_success_with_untrusted_author_excludes_issue_without_degrading_snapshot(self):
        trusted_issue = make_issue(10, status="Ready", touches="scripts/a.py")
        untrusted_issue = make_issue(20, status="Ready", touches="scripts/b.py")
        untrusted_issue["user"] = {"login": "outsider"}
        untrusted_issue["author_association"] = "NONE"

        repo = FakeRepo(issues=[trusted_issue, untrusted_issue])
        with wired_repo(repo):
            snapshot = fls.evaluate_factory_loop_snapshot(".")

        self.assertFalse(snapshot["degraded"])
        self.assertEqual(snapshot["state"], "waiting")
        self.assertEqual(snapshot["exit_code"], fls.EXIT_WAITING)
        claimable = [c["issue"] for c in snapshot["claimable_work"]]
        self.assertEqual(claimable, [10])
        self.assertIn(20, snapshot["candidate_diagnostics"]["missing_touches"])

    def test_board_inventory_restricts_counts_and_indexing_to_issue_content_with_integer_numbers(self):
        issue101 = make_issue(101, status="Ready", touches="scripts/a.py")
        issue102 = make_issue(102, status="Backlog", touches="scripts/b.py")
        repo = FakeRepo(
            issues=[issue101, issue102],
            board_items=[
                {"status": "Ready", "content": {"type": "Issue", "number": 101, "repository": "gillella/Aru_Agentic_SDLC"}},
                {"status": "In Review", "content": {"type": "PullRequest", "number": 101, "repository": "gillella/Aru_Agentic_SDLC"}},
                {"status": "In Progress", "content": {"type": "PullRequest", "number": 201, "repository": "gillella/Aru_Agentic_SDLC"}},
                {"status": "Ready", "content": {"type": "DraftIssue", "title": "Draft Idea", "repository": "gillella/Aru_Agentic_SDLC"}},
                {"status": "Ready", "content": {"number": "not-an-int", "repository": "gillella/Aru_Agentic_SDLC"}},
                {"status": "Backlog", "content": {"type": "Discussion", "number": 301, "repository": "gillella/Aru_Agentic_SDLC"}},
                {"status": "Ready", "content": {"type": "Issue", "number": 50, "repository": "other/repo"}},
                {"status": "Backlog", "content": {"number": 102, "repository": "gillella/Aru_Agentic_SDLC"}},
            ],
        )
        with wired_repo(repo):
            snapshot = fls.evaluate_factory_loop_snapshot(".")

        self.assertFalse(snapshot["degraded"])
        self.assertEqual(snapshot["state"], "waiting")
        self.assertEqual(snapshot["exit_code"], fls.EXIT_WAITING)
        self.assertEqual(snapshot["board"]["status_counts"], {"Backlog": 1, "Ready": 1})
        self.assertEqual(snapshot["open_issues"][0]["board_status"], "Ready")
        self.assertEqual(snapshot["open_issues"][1]["board_status"], "Backlog")

    def test_collaborator_lookup_exception_propagates_degraded_blocked_candidate_evaluation(self):
        issue1 = make_issue(10, status="Ready")
        repo = FakeRepo(issues=[issue1])
        with wired_repo(repo), patch("factory_loop_snapshot.repository_trusted_logins", side_effect=RuntimeError("gh api network timeout")):
            snapshot = fls.evaluate_factory_loop_snapshot(".")

        self.assertTrue(snapshot["degraded"])
        self.assertEqual(snapshot["state"], "blocked")
        self.assertEqual(snapshot["exit_code"], fls.EXIT_BLOCKED)
        self.assertEqual(snapshot["claimable_work"], [])
        self.assertTrue(any("Could not resolve trusted collaborator logins" in e for e in snapshot["errors"]))


if __name__ == "__main__":
    unittest.main()

