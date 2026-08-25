import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import common  # noqa: E402
from common import (  # noqa: E402
    add_issue_to_project,
    attach_issue_to_governed_project,
    parse_touches,
    paths_overlap,
    select_governed_project_items,
    select_governed_projects,
)


class TouchMetadataTests(unittest.TestCase):
    def test_parser_ignores_prose_and_reads_metadata_line(self):
        body = """The new `touches:` declaration prevents collisions.

## Dependencies
touches: scripts/*, tests/test_common.py
"""

        self.assertEqual(
            parse_touches(body),
            ["scripts/*", "tests/test_common.py"],
        )

    def test_path_overlap_is_conservative_without_prefix_confusion(self):
        self.assertTrue(paths_overlap("src/*", "src/app/main.py"))
        self.assertFalse(paths_overlap("src/*", "src2/main.py"))
        self.assertFalse(paths_overlap("README.md", "README.md.bak"))


class GovernedProjectSelectionTests(unittest.TestCase):
    def project_item(self, title, *repositories):
        return {
            "id": title,
            "project": {
                "title": title,
                "repositories": {
                    "nodes": [{"nameWithOwner": repo} for repo in repositories]
                },
            },
        }

    def test_exact_governed_board_wins_over_other_linked_projects(self):
        items = [
            self.project_item("Team Roadmap", "octocat/widgets"),
            self.project_item("widgets Board", "octocat/widgets"),
        ]

        selected = select_governed_project_items(items, "octocat/widgets")

        self.assertEqual([item["id"] for item in selected], ["widgets Board"])

    def test_multiple_linked_projects_without_governed_name_fail_closed(self):
        items = [
            self.project_item("Team Roadmap", "octocat/widgets"),
            self.project_item("Release Tracker", "octocat/widgets"),
        ]

        self.assertEqual(select_governed_project_items(items, "octocat/widgets"), [])

    def test_project_resolution_uses_the_same_governed_board_contract(self):
        projects = [
            self.project_item("Team Roadmap", "octocat/widgets")["project"],
            self.project_item("widgets Board", "octocat/widgets")["project"],
        ]

        selected = select_governed_projects(projects, "octocat/widgets")

        self.assertEqual([project["title"] for project in selected], ["widgets Board"])


class GovernedProjectAttachmentTests(unittest.TestCase):
    def project(self):
        return {
            "id": "PROJECT_7",
            "number": 7,
            "title": "widgets Board",
            "owner": {"login": "octocat"},
            "repositories": {
                "nodes": [{"nameWithOwner": "octocat/widgets"}],
            },
        }

    @patch.object(common, "add_issue_to_project", return_value=True)
    @patch.object(common, "get_repo_projects")
    @patch.object(common, "get_issue_project_items", return_value=[])
    @patch.object(common, "get_repo_slug", return_value="octocat/widgets")
    def test_resolves_and_attaches_without_hardcoding(
        self, _slug, _items, projects, add
    ):
        projects.return_value = [self.project()]

        self.assertTrue(attach_issue_to_governed_project(42))

        add.assert_called_once_with(42, 7, "octocat")

    @patch.object(common, "add_issue_to_project")
    @patch.object(common, "get_repo_projects")
    @patch.object(common, "get_issue_project_items")
    @patch.object(common, "get_repo_slug", return_value="octocat/widgets")
    def test_already_attached_is_a_no_op(self, _slug, items, projects, add):
        project = self.project()
        projects.return_value = [project]
        items.return_value = [{"id": "ITEM_42", "project": project}]

        self.assertTrue(attach_issue_to_governed_project(42))

        add.assert_not_called()

    @patch.object(common, "run_cmd", return_value=(1, "", "permission denied"))
    @patch.object(common, "get_repo_slug", return_value="octocat/widgets")
    def test_attachment_failure_names_the_manual_remedy(self, _slug, _run):
        with patch("sys.stderr") as stderr:
            self.assertFalse(add_issue_to_project(42, 7, "octocat"))

        rendered = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn(
            "gh project item-add 7 --owner octocat "
            "--url https://github.com/octocat/widgets/issues/42",
            rendered,
        )

    @patch.object(common, "run_cmd", return_value=(0, "", ""))
    @patch.object(common, "attach_issue_to_governed_project", return_value=True)
    @patch.object(common, "get_repo_slug", return_value="octocat/widgets")
    @patch.object(common, "get_issue_project_items")
    def test_status_move_attaches_an_unboarded_issue_first(
        self, items, _slug, attach, _run
    ):
        project = self.project()
        project["field"] = {
            "id": "STATUS_FIELD",
            "options": [{"id": "READY", "name": "Ready"}],
        }
        items.side_effect = [[], [{"id": "ITEM_42", "project": project}]]

        self.assertTrue(common.set_board_status(42, "Ready"))

        attach.assert_called_once_with(42)


if __name__ == "__main__":
    unittest.main()


class BlankTouchesRegressionTests(unittest.TestCase):
    """`\\s` matches newlines, so a lazy capture ran past the line ending."""

    def test_blank_declaration_does_not_swallow_the_next_line(self):
        body = "depends-on:\ntouches:\nparallel-eligible: true\n"
        self.assertEqual(parse_touches(body), [])

    def test_blank_declaration_at_end_of_body(self):
        self.assertEqual(parse_touches("touches:"), [])

    def test_real_declaration_still_parses(self):
        self.assertEqual(
            parse_touches("touches: src/a.py, docs/b.md\nparallel-eligible: true"),
            ["src/a.py", "docs/b.md"])

    def test_markdown_emphasis_is_tolerated(self):
        self.assertEqual(parse_touches("**touches:** a.py, b.py"), ["a.py", "b.py"])

    def test_parenthesised_prose_is_not_a_path(self):
        self.assertEqual(parse_touches("touches: (github settings only)"), [])

    def test_glob_paths_remain_valid(self):
        self.assertEqual(parse_touches("touches: scripts/*, tests/test_common.py"), [
            "scripts/*", "tests/test_common.py",
        ])
        self.assertEqual(parse_touches("touches: **"), ["**"])
        self.assertEqual(parse_touches("touches: `**`"), ["**"])
        self.assertEqual(parse_touches("touches: **/*.py"), ["**/*.py"])
        self.assertEqual(parse_touches("touches: **/module.py"), ["**/module.py"])

    def test_traversal_and_absolute_paths_are_rejected(self):
        self.assertEqual(parse_touches("touches: ../etc/passwd, scripts/common.py"), [
            "scripts/common.py",
        ])
        self.assertEqual(parse_touches("touches: /etc/passwd"), [])
        self.assertEqual(parse_touches("touches: C:\\Windows\\System32"), [])
        self.assertEqual(parse_touches("touches: ~/secret"), [])
        self.assertEqual(parse_touches("touches: scripts/foo/../../etc/passwd"), [])
        self.assertEqual(parse_touches("touches: ./scripts/common.py"), [])
        self.assertEqual(parse_touches("touches: scripts/./common.py"), [])

    def test_command_like_declaration_is_honoured_as_empty(self):
        self.assertEqual(parse_touches("touches: scripts/a.py; rm -rf /"), [])
        self.assertEqual(parse_touches("touches: scripts/a.py && wget evil"), [])


class MetadataTrustTests(unittest.TestCase):
    def test_owner_author_is_trusted_and_outsider_is_not(self):
        from common import is_trusted_metadata_author
        self.assertTrue(is_trusted_metadata_author(
            {"author": {"login": "gillella"}}, owner="gillella"))
        self.assertFalse(is_trusted_metadata_author(
            {"author": {"login": "attacker"}}, owner="gillella"))

    def test_missing_identity_fails_closed(self):
        from common import is_trusted_metadata_author
        self.assertFalse(is_trusted_metadata_author({}, owner="gillella"))
        self.assertFalse(is_trusted_metadata_author(
            {"author": {"login": "attacker"}}, owner=None))
        self.assertFalse(is_trusted_metadata_author(
            {"author": {"login": ""}}, owner="gillella"))

    def test_org_collaborator_is_trusted_without_owner_login_match(self):
        from common import is_trusted_metadata_author
        outsider = {"author": {"login": "alice"}}
        self.assertFalse(is_trusted_metadata_author(outsider, owner="acme-corp"))
        self.assertTrue(is_trusted_metadata_author(
            outsider, owner="acme-corp", trusted_logins={"alice", "bob"}))
        self.assertTrue(is_trusted_metadata_author(
            {"author": {"login": "alice"}, "authorAssociation": "MEMBER"},
            owner="acme-corp",
        ))

    def test_trusted_rewrite_state_unlocks_outsider_metadata(self):
        from common import TRUSTED_REWRITE_LABEL, is_trusted_metadata_author
        outsider = {
            "author": {"login": "attacker"},
            "labels": [{"name": "status:ready"}],
        }
        self.assertFalse(is_trusted_metadata_author(outsider, owner="gillella"))
        rewritten = {
            **outsider,
            "labels": [
                {"name": "status:ready"},
                {"name": TRUSTED_REWRITE_LABEL},
            ],
        }
        self.assertTrue(is_trusted_metadata_author(rewritten, owner="gillella"))
        edited = {
            "author": {"login": "attacker"},
            "editor": {"login": "gillella"},
        }
        self.assertTrue(is_trusted_metadata_author(edited, owner="gillella"))
        relabeled_then_hijacked = {
            **rewritten,
            "editor": {"login": "attacker"},
        }
        self.assertFalse(is_trusted_metadata_author(
            relabeled_then_hijacked, owner="gillella"))
        lookup_failed = {
            **rewritten,
            "trustIdentityResolved": False,
        }
        self.assertFalse(is_trusted_metadata_author(
            lookup_failed, owner="gillella"))

    @patch.object(common, "run_cmd", return_value=(1, "", "http 403"))
    @patch.object(common, "get_repo_slug", return_value="acme-corp/widgets")
    def test_collaborator_lookup_failure_is_unresolved(self, _slug, _run):
        self.assertIsNone(common.repository_trusted_logins())

    @patch.object(common, "run_cmd", return_value=(0, "alice\nbob\n", ""))
    @patch.object(common, "get_repo_slug", return_value="acme-corp/widgets")
    def test_collaborator_lookup_includes_owner_and_actors(self, _slug, _run):
        self.assertEqual(
            common.repository_trusted_logins(),
            {"acme-corp", "alice", "bob"},
        )

class ProseAndCodeBlockExclusionTests(unittest.TestCase):
    """#294: line-anchored metadata must not treat code or prose as paths.

    A fenced block begins at column 0 and an indented block is indistinguishable
    from a declaration to a ``^[ \t]*`` anchor, and a prose sentence that
    happens to mention ``touches:`` at line start must never become a
    reservation.
    """

    TEMPLATE = (
        "## Feature Description\n"
        "Issues follow this template:\n\n"
        "```\n"
        "depends-on: none\n"
        "touches: scripts/EXAMPLE.py, tests/test_EXAMPLE.py\n"
        "```\n\n"
        "## Dependencies\n"
        "depends-on: #288, #289\n"
        "touches: scripts/intake.py, tests/test_intake.py\n"
    )

    def test_real_declaration_wins_over_fenced_example(self):
        self.assertEqual(
            parse_touches(self.TEMPLATE),
            ["scripts/intake.py", "tests/test_intake.py"],
        )

    def test_tilde_fences_are_handled(self):
        self.assertEqual(
            parse_touches(self.TEMPLATE.replace("```", "~~~")),
            ["scripts/intake.py", "tests/test_intake.py"],
        )

    def test_indented_code_block_is_ignored(self):
        body = (
            "## Example\n\n"
            "    touches: scripts/EXAMPLE.py\n"
            "    depends-on: none\n\n"
            "## Dependencies\n"
            "touches: scripts/real.py\n"
        )
        self.assertEqual(parse_touches(body), ["scripts/real.py"])

    def test_tab_indented_block_is_ignored(self):
        body = "\ttouches: scripts/EXAMPLE.py\n\n## Dependencies\ntouches: scripts/real.py\n"
        self.assertEqual(parse_touches(body), ["scripts/real.py"])

    def test_em_dash_prose_tail_is_rejected(self):
        # Observed on #294's own first draft: the parse returned the em-dash
        # prose fragment as a reserved path.
        self.assertEqual(
            parse_touches("`touches:` declaration \u2014 every issue discusses it"),
            [],
        )

    def test_whitespace_inside_a_value_is_rejected(self):
        self.assertEqual(parse_touches("touches: scripts/foo bar.py"), [])
        self.assertEqual(parse_touches("touches: scripts/a.py, b c.py"),
                         ["scripts/a.py"])

    def test_quoted_wrapper_is_rejected(self):
        self.assertEqual(parse_touches('touches: "scripts/foo.py"'), [])

    def test_unterminated_fence_fails_closed(self):
        # No declaration makes an issue non-claimable; a wrong one causes two
        # agents to collide. Prefer the former.
        self.assertEqual(parse_touches("```\ntouches: scripts/EXAMPLE.py"), [])

    def test_strip_code_blocks_preserves_line_count(self):
        body = "a\n```\nb\n```\nc\n    indented"
        stripped = common.strip_code_blocks(body)
        # split("\n") not splitlines(): the trailing blank line that represents
        # the blanked indented block must survive the round-trip.
        self.assertEqual(stripped.split("\n"), ["a", "", "", "", "c", ""])
        self.assertEqual(len(stripped.split("\n")), len(body.split("\n")))
