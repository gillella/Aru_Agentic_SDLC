# +62 for the #406 Sourcery fail-closed regression tests: unreadable worktree
# reads, malformed pull-request rows, malformed board items, and the open-review
# reason its Slack caller filters for.
# line-ceiling: 453
"""Compact read-only status (#406).

The fleet supervisor these tests used to cover is gone. What is left has to
answer one question for a returning operator -- what is open, who holds it,
which worktrees are still on disk -- from authoritative GitHub and Git state,
and it has to refuse to answer at all when that state is incomplete.

Every test here is hermetic: it replaces the single subprocess chokepoint every
GitHub and Git read funnels through, so no test touches the network, the real
board, or the real checkout.
"""

import contextlib
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import common
import fleet_status as fs
import picker_board_inventory as pbi

SLUG = "gillella/Aru_Agentic_SDLC"

# Every live checkout lists at least its own main worktree, so this -- not the
# empty string -- is what a healthy `git worktree list --porcelain` looks like.
MAIN_WORKTREE = "worktree /repo\nHEAD abc\nbranch refs/heads/main\n"


def issue(number, *, status="Ready", agent=None, title="t"):
    labels = [{"name": f"status:{status.lower().replace(' ', '-')}"}]
    if agent:
        labels.append({"name": f"agent:{agent}"})
    return {
        "number": number, "title": title, "labels": labels, "assignees": [],
        "state": "open", "user": {"login": "gillella"}, "body": "",
        "updated_at": "2026-08-26T00:00:00Z", "author_association": "OWNER",
    }


def pull(number, *, branch="chore/issue-1-x", author=None, draft=False):
    """One row as the paginated REST pull-request endpoint returns it."""
    labels = [{"name": f"author:{author}"}] if author else []
    return {
        "number": number, "title": f"PR {number}", "draft": draft,
        "labels": labels, "head": {"ref": branch, "sha": "deadbeef"},
        "updated_at": "2026-08-26T00:00:00Z",
    }


class FakeRepo:
    """Answers the exact command set one status run is allowed to issue."""

    def __init__(self, *, issues=(), prs=(), worktrees=None, board=None,
                 board_items=None, projects=None, slug=SLUG, fail=()):
        self.issues = list(issues)
        self.prs = list(prs)
        self.worktrees = MAIN_WORKTREE if worktrees is None else worktrees
        self.slug = slug
        self.board_items = board_items
        self.fail = set(fail)
        self.projects = projects if projects is not None else [{
            "id": "PVT_1", "number": 7, "title": "Aru_Agentic_SDLC Board",
            "owner": {"login": "gillella"},
            "repositories": {"nodes": [{"nameWithOwner": SLUG}]},
        }]
        # board maps issue number -> board status; default mirrors the labels.
        self.board = board if board is not None else {
            item["number"]: item["labels"][0]["name"].split(":", 1)[1].title()
            for item in self.issues
        }
        self.commands = []

    @property
    def gh_calls(self):
        return [cmd for cmd in self.commands if cmd[0] == "gh"]

    def _item_list(self):
        items = self.board_items if self.board_items is not None else [
            {"status": status,
             "content": {"number": number, "repository": self.slug}}
            for number, status in self.board.items()
        ]
        return json.dumps({"items": items, "totalCount": len(items)})

    def __call__(self, cmd, check=True, **kwargs):
        cmd = list(cmd)
        self.commands.append(cmd)
        joined = " ".join(cmd)
        if "slug" in self.fail and cmd[:2] == ["git", "remote"]:
            return (1, "", "no origin")
        if cmd[:2] == ["git", "remote"]:
            return (0, f"git@github.com:{self.slug}.git\n", "")
        if cmd[:3] == ["git", "worktree", "list"]:
            if "worktrees" in self.fail:
                return (1, "", "fatal: not a git repository")
            return (0, self.worktrees, "")
        if "issues?state=open" in joined:
            if "issues" in self.fail:
                return (1, "", "api down")
            return (0, "\n".join(json.dumps(row) for row in self.issues), "")
        if cmd[:3] == ["gh", "api", "graphql"]:
            if "projects" in self.fail:
                return (1, "", "api down")
            return (0, json.dumps(
                {"data": {"repository": {"projectsV2": {"nodes": self.projects}}}}), "")
        if cmd[:3] == ["gh", "project", "item-list"]:
            if "board" in self.fail:
                return (1, "", "api down")
            return (0, self._item_list(), "")
        if "pulls?state=open" in joined:
            if "prs" in self.fail:
                return (1, "", "api down")
            return (0, "\n".join(json.dumps(row) for row in self.prs), "")
        raise AssertionError(f"unexpected command: {cmd}")


@contextlib.contextmanager
def wired(repo):
    """Route every read in the status path through one fake runner."""
    with patch.object(common, "run_cmd", repo), \
         patch.object(pbi, "run_cmd", repo), \
         patch.object(fs, "run_cmd", repo), \
         patch("os.chdir"), patch("os.path.isdir", return_value=True):
        yield repo


class ReadOnlyContractTests(unittest.TestCase):
    def test_status_issues_no_mutating_command(self):
        """A status command that can write is a supervisor, not a report."""
        repo = FakeRepo(issues=[issue(1)], prs=[pull(9)])
        with wired(repo):
            fs.evaluate_fleet_status(".")
        mutating = ("edit", "create", "merge", "close", "comment", "delete", "item-edit")
        for cmd in repo.commands:
            self.assertFalse(
                set(cmd) & set(mutating),
                f"status issued a mutating command: {cmd}",
            )

    def test_no_supervisor_or_telemetry_surface_remains(self):
        for name in (
            "detect_stall", "notify_stall", "build_merge_queue", "resolve_fleet_size",
            "discover_fleet_size", "fetch_ci_history", "collect_codebase_health",
            "registered_agent_count", "build_operator_screen", "resolve_ready_target",
        ):
            self.assertFalse(hasattr(fs, name), f"removed surface still present: {name}")

    def test_cli_rejects_every_retired_supervisor_flag(self):
        """A flag that still parses is a contract the command no longer keeps."""
        for flag in ("--queue", "--queue-json", "--metrics", "--fleet-size",
                     "--ready-target", "--stall-hours", "--stall-agent"):
            with self.subTest(flag=flag):
                repo = FakeRepo()
                with wired(repo), patch("sys.argv", ["fleet_status.py", flag]), \
                     patch("sys.stderr"):
                    with self.assertRaises(SystemExit) as raised:
                        fs.main()
                self.assertEqual(raised.exception.code, 2)
                self.assertEqual(repo.commands, [])


class RecoveryFactTests(unittest.TestCase):
    def test_open_issue_reports_board_status_and_claim(self):
        repo = FakeRepo(issues=[issue(406, status="In Progress", agent="claude-1")],
                        board={406: "In Progress"})
        with wired(repo):
            status = fs.evaluate_fleet_status(".")
        self.assertEqual(status["state"], "waiting")
        self.assertEqual(status["exit_code"], fs.EXIT_WAITING)
        row = status["issues"][0]
        self.assertEqual(row["board_status"], "In Progress")
        self.assertEqual(row["agent"], "claude-1")
        self.assertFalse(row["drifted"])
        self.assertIn({"type": "issue", "number": 406, "agent": "claude-1"},
                      status["claims"])

    def test_label_and_board_drift_is_reported(self):
        """The mismatch is how a session that died mid-transition is found."""
        repo = FakeRepo(issues=[issue(406, status="In Progress")], board={406: "Ready"})
        with wired(repo):
            status = fs.evaluate_fleet_status(".")
        self.assertTrue(status["issues"][0]["drifted"])
        self.assertTrue(any("drifts from board status" in r for r in status["reasons"]))

    def test_in_review_issue_is_not_reported_as_a_live_claim(self):
        """claimed_by() drops the label at In Review; status must not re-add it."""
        repo = FakeRepo(issues=[issue(406, status="In Review", agent="claude-1")],
                        board={406: "In Review"})
        with wired(repo):
            status = fs.evaluate_fleet_status(".")
        self.assertIsNone(status["issues"][0]["agent"])
        self.assertEqual(status["claims"], [])

    def test_open_pr_reports_identity_and_author_claim(self):
        repo = FakeRepo(prs=[pull(458, branch="chore/issue-406-x", author="claude-1")])
        with wired(repo):
            status = fs.evaluate_fleet_status(".")
        row = status["pull_requests"][0]
        self.assertEqual(row["number"], 458)
        self.assertEqual(row["branch"], "chore/issue-406-x")
        self.assertEqual(row["head"], "deadbeef")
        self.assertEqual(row["author"], "claude-1")
        self.assertIn({"type": "pr", "number": 458, "agent": "claude-1"},
                      status["claims"])

    def test_worktree_for_a_closed_issue_is_flagged_stale(self):
        porcelain = (
            "worktree /repo\nHEAD abc\nbranch refs/heads/main\n\n"
            "worktree /repo/.worktrees/chore-issue-99__claude-1\n"
            "HEAD def\nbranch refs/heads/chore/issue-99-old\n"
        )
        repo = FakeRepo(issues=[issue(406)], worktrees=porcelain)
        with wired(repo):
            status = fs.evaluate_fleet_status(".")
        rows = {row["branch"]: row for row in status["worktrees"]}
        self.assertTrue(rows["chore/issue-99-old"]["stale"])
        self.assertEqual(rows["chore/issue-99-old"]["issue"], 99)
        self.assertEqual(rows["chore/issue-99-old"]["agent"], "claude-1")
        self.assertFalse(rows["main"]["stale"])
        self.assertIsNone(rows["main"]["issue"])
        self.assertTrue(
            any("remains for closed issue #99" in r for r in status["reasons"]))

    def test_empty_repository_is_complete(self):
        repo = FakeRepo()
        with wired(repo):
            status = fs.evaluate_fleet_status(".")
        self.assertEqual(status["state"], "complete")
        self.assertEqual(status["exit_code"], fs.EXIT_COMPLETE)
        self.assertEqual(status["open_issues_count"], 0)
        self.assertEqual(status["open_prs_count"], 0)

    def test_worktree_without_an_issue_branch_never_forces_waiting(self):
        repo = FakeRepo(worktrees="worktree /repo\nHEAD abc\nbranch refs/heads/main\n")
        with wired(repo):
            status = fs.evaluate_fleet_status(".")
        self.assertEqual(status["state"], "complete")


class FailClosedTests(unittest.TestCase):
    def assert_failed(self, status, state, code):
        self.assertEqual(status["state"], state)
        self.assertEqual(status["exit_code"], code)
        self.assertIsNone(status["open_issues_count"])
        self.assertEqual(status["issues"], [])
        self.assertEqual(status["claims"], [])
        self.assertTrue(status["reasons"])

    def test_unreadable_issue_list_fails_closed(self):
        repo = FakeRepo(issues=[issue(1)], fail={"issues"})
        with wired(repo):
            status = fs.evaluate_fleet_status(".")
        self.assert_failed(status, "error", fs.EXIT_ERROR)

    def test_unreadable_pull_request_list_fails_closed(self):
        repo = FakeRepo(prs=[pull(1)], fail={"prs"})
        with wired(repo):
            status = fs.evaluate_fleet_status(".")
        self.assert_failed(status, "error", fs.EXIT_ERROR)

    def test_unresolvable_repository_fails_closed(self):
        repo = FakeRepo(fail={"slug"})
        with wired(repo):
            status = fs.evaluate_fleet_status(".")
        self.assert_failed(status, "error", fs.EXIT_ERROR)

    def test_unreadable_board_is_blocked_not_complete(self):
        repo = FakeRepo(issues=[issue(1)], fail={"board"})
        with wired(repo):
            status = fs.evaluate_fleet_status(".")
        self.assert_failed(status, "blocked", fs.EXIT_BLOCKED)

    def test_ambiguous_board_is_blocked(self):
        second = {"id": "PVT_2", "number": 8, "title": "Aru_Agentic_SDLC Board",
                  "owner": {"login": "gillella"},
                  "repositories": {"nodes": [{"nameWithOwner": SLUG}]}}
        repo = FakeRepo(issues=[issue(1)])
        repo.projects = repo.projects + [second]
        with wired(repo):
            status = fs.evaluate_fleet_status(".")
        self.assert_failed(status, "blocked", fs.EXIT_BLOCKED)

    def test_open_issue_missing_from_the_board_is_blocked(self):
        """A partial snapshot cannot prove the board; reporting it would lie."""
        repo = FakeRepo(issues=[issue(1), issue(2)], board={1: "Ready"})
        with wired(repo):
            status = fs.evaluate_fleet_status(".")
        self.assert_failed(status, "blocked", fs.EXIT_BLOCKED)

    def test_unreadable_worktree_list_fails_closed(self):
        """An unreadable checkout is not an empty one, and must not read ok.

        Both shapes are the same failure: `git worktree list --porcelain`
        returned nothing usable, and an otherwise empty repository would
        otherwise be reported COMPLETE while local state is unknown.
        """
        for label, kwargs in (("nonzero exit", {"fail": {"worktrees"}}),
                              ("no output", {"worktrees": ""})):
            with self.subTest(case=label):
                repo = FakeRepo(**kwargs)
                with wired(repo):
                    status = fs.evaluate_fleet_status(".")
                self.assert_failed(status, "error", fs.EXIT_ERROR)
                self.assertEqual(status["worktrees"], [])

    def test_malformed_pull_request_row_fails_closed(self):
        """Every row the report indexes is validated before it is indexed."""
        head = {"ref": "chore/issue-1-x", "sha": "deadbeef"}
        for row in ("not-a-dict", {"title": "no number", "head": head},
                    {"number": "458", "head": head},
                    {"number": 458, "labels": None, "head": head},
                    {"number": 458, "labels": ["author:claude-1"], "head": head},
                    {"number": 458, "labels": [{"name": 7}], "head": head},
                    {"number": 458, "labels": [], "head": "not-an-object"}):
            with self.subTest(row=row):
                repo = FakeRepo(prs=[row])
                with wired(repo):
                    status = fs.evaluate_fleet_status(".")
                self.assert_failed(status, "error", fs.EXIT_ERROR)
                self.assertEqual(status["pull_requests"], [])

    def test_malformed_board_item_is_blocked_not_raised(self):
        """The board read is delegated, so the delegate fails closed as well."""
        for items in (["not-an-object"],
                      [{"status": "Ready", "content": "not-an-object"}],
                      [{"status": "Ready", "content": {"number": {"n": 1}}}]):
            with self.subTest(items=items):
                repo = FakeRepo(issues=[issue(1)], board_items=items)
                with wired(repo):
                    status = fs.evaluate_fleet_status(".")
                self.assert_failed(status, "blocked", fs.EXIT_BLOCKED)

    def test_missing_repository_directory_fails_closed(self):
        repo = FakeRepo()
        with patch.object(common, "run_cmd", repo), \
             patch("os.path.isdir", return_value=False):
            status = fs.evaluate_fleet_status("/no/such/repo")
        self.assert_failed(status, "error", fs.EXIT_ERROR)
        self.assertEqual(repo.commands, [])


class QueryBudgetTests(unittest.TestCase):
    """#406's query-budget note: cost must not scale with the backlog."""

    def _run(self, count):
        repo = FakeRepo(issues=[issue(n) for n in range(1, count + 1)])
        with wired(repo):
            status = fs.evaluate_fleet_status(".")
        self.assertEqual(status["open_issues_count"], count)
        return repo

    def test_command_count_does_not_grow_with_open_issue_count(self):
        small, large = self._run(3), self._run(60)
        self.assertEqual(len(small.gh_calls), len(large.gh_calls))
        self.assertEqual(len(small.commands), len(large.commands))

    def test_the_board_is_read_as_one_complete_snapshot(self):
        repo = self._run(60)
        item_lists = [c for c in repo.gh_calls if c[:3] == ["gh", "project", "item-list"]]
        self.assertEqual(len(item_lists), 1)
        # One page large enough that the reader's totalCount check is meaningful.
        self.assertEqual(item_lists[0][item_lists[0].index("--limit") + 1], "1000")
        self.assertIn("--format", item_lists[0])

    def test_no_per_issue_board_query_is_made(self):
        """The N+1 scan this command used to run exhausted the shared quota."""
        repo = self._run(60)
        for cmd in repo.gh_calls:
            self.assertNotIn("issueNumber", " ".join(cmd))
        graphql = [c for c in repo.gh_calls if c[:3] == ["gh", "api", "graphql"]]
        self.assertEqual(len(graphql), 1)


class RepoDirAndJsonTests(unittest.TestCase):
    def test_repo_dir_is_entered_and_always_restored(self):
        repo = FakeRepo(issues=[issue(1)])
        with patch.object(common, "run_cmd", repo), \
             patch.object(pbi, "run_cmd", repo), \
             patch.object(fs, "run_cmd", repo), \
             patch("os.path.isdir", return_value=True), \
             patch("os.chdir") as chdir:
            fs.evaluate_fleet_status("/elsewhere/repo")
        self.assertEqual(chdir.call_args_list[0].args[0], "/elsewhere/repo")
        self.assertEqual(len(chdir.call_args_list), 2)

    def test_repo_dir_is_restored_after_a_failure(self):
        repo = FakeRepo(fail={"slug"})
        with patch.object(common, "run_cmd", repo), \
             patch("os.path.isdir", return_value=True), \
             patch("os.chdir") as chdir:
            fs.evaluate_fleet_status("/elsewhere/repo")
        self.assertEqual(len(chdir.call_args_list), 2)

    def test_json_output_is_parsable_and_carries_the_exit_code(self):
        repo = FakeRepo(issues=[issue(406, agent="claude-1")])
        with wired(repo), patch("sys.argv", ["fleet_status.py", "--json"]), \
             patch("builtins.print") as printed:
            with self.assertRaises(SystemExit) as raised:
                fs.main()
        payload = json.loads(printed.call_args.args[0])
        self.assertEqual(payload["state"], "waiting")
        self.assertEqual(raised.exception.code, payload["exit_code"])
        self.assertEqual(payload["claims"][0]["agent"], "claude-1")

    def test_text_output_names_the_state_and_the_worktrees(self):
        repo = FakeRepo(
            issues=[issue(406)],
            worktrees="worktree /repo/.worktrees/w\nbranch refs/heads/chore/issue-406-x\n",
        )
        with wired(repo):
            text = fs.format_status(fs.evaluate_fleet_status("."))
        self.assertIn("State: WAITING", text)
        self.assertIn("/repo/.worktrees/w", text)
        self.assertIn("Issue #406", text)


class SlackCompatibilityTests(unittest.TestCase):
    """slack_control_room.py imports this name and reads these keys (#415 owns it)."""

    def test_evaluate_fleet_status_keeps_the_keys_its_caller_reads(self):
        repo = FakeRepo(issues=[issue(1)], prs=[pull(2)])
        with wired(repo):
            status = fs.evaluate_fleet_status(".")
        for key in ("state", "summary", "reasons", "open_issues_count", "open_prs_count"):
            self.assertIn(key, status)
        self.assertIsInstance(status["reasons"], list)

    def test_open_pull_requests_stay_visible_as_open_review_work(self):
        """status_text() picks open review work by filtering reasons for 'review'.

        A compact status that never says the word renders "open review work:
        none" while PRs sit open, hiding the one fact the operator opened the
        status to see.
        """
        repo = FakeRepo(prs=[pull(458, author="claude-1"), pull(459, draft=True)])
        with wired(repo):
            status = fs.evaluate_fleet_status(".")
        matched = [line for line in status["reasons"] if "review" in line.lower()]
        self.assertEqual(len(matched), 2)
        self.assertTrue(any("#458" in line and "claude-1" in line for line in matched))
        self.assertTrue(any("#459" in line and "draft" in line for line in matched))


if __name__ == "__main__":
    unittest.main()
