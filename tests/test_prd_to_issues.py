import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import prd_to_issues as pti  # noqa: E402
import triage_backlog  # noqa: E402


class PrdToIssuesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        for directory in ("src", "tests", "docs"):
            (self.repo / directory).mkdir()
        self.inventory = (
            "docs/guide.md",
            "src/core.py",
            "src/nested/deep.py",
            "src/ui.py",
            "tests/test_core.py",
            "tests/test_ui.py",
        )
        for relative in self.inventory:
            path = self.repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("tracked\n")

    def manifest(self):
        return {
            "source_prd": 82,
            "epics": [
                {
                    "key": "phase-one",
                    "title": "epic: phase one",
                    "summary": "Foundation delivery.",
                    "phase": "1 - Foundation",
                }
            ],
            "issues": [
                {
                    "key": "foundation",
                    "title": "feat: foundation",
                    "summary": "As a user, I can use the foundation.",
                    "type": "feat",
                    "priority": "p1",
                    "epic": "phase-one",
                    "phase": "1 - Foundation",
                    "change_targets": [
                        {"path": "src/core.py", "kind": "existing"},
                        {"path": "tests/test_core.py", "kind": "existing"},
                    ],
                    "depends_on": [],
                    "acceptance_criteria": [
                        {"predicate": "Foundation works.", "verify": "python3 -m unittest tests.test_core"}
                    ],
                    "decision_boundaries": ["Keep the public API stable."],
                    "non_goals": ["No UI changes."],
                    "verification": ["python3 -m unittest tests.test_core"],
                },
                {
                    "key": "interface",
                    "title": "feat: interface",
                    "summary": "As a user, I can use the interface.",
                    "type": "feat",
                    "priority": "p2",
                    "epic": "phase-one",
                    "change_targets": [
                        {"path": "src/ui.py", "kind": "existing"},
                        {"path": "tests/test_ui.py", "kind": "existing"},
                    ],
                    "depends_on": ["foundation"],
                    "acceptance_criteria": [
                        {"predicate": "Interface works.", "verify": "python3 -m unittest tests.test_ui"}
                    ],
                    "decision_boundaries": ["Use the foundation API."],
                    "non_goals": ["No documentation changes."],
                    "verification": ["python3 -m unittest tests.test_ui"],
                },
            ],
        }

    def test_skill_declares_governed_decomposition_contract(self):
        skill = Path(__file__).resolve().parents[1] / "skills" / "prd-to-issues" / "SKILL.md"
        content = skill.read_text()
        self.assertIn("name: prd-to-issues", content)
        self.assertIn("READY_FOR_PLANNING", content)
        self.assertIn("prd_to_issues.py", content)
        self.assertIn("--require-board", content)
        self.assertIn("triage-backlog", content)

    def test_decomposition_is_topological_and_renders_intake_contract(self):
        plan = pti.prepare_plan(self.manifest(), self.repo, self.inventory)

        self.assertEqual([issue.key for issue in plan.issues], ["foundation", "interface"])
        self.assertTrue(plan.issues[0].parallel_eligible)
        self.assertFalse(plan.issues[1].parallel_eligible)
        body = pti.render_issue_body(
            plan.issues[1], plan.source_prd, {"foundation": 101}, 100
        )
        self.assertIn("- [ ] Interface works.", body)
        self.assertIn("(verify: `python3 -m unittest tests.test_ui`)", body)
        self.assertIn("depends-on: #101", body)
        self.assertIn("touches: src/ui.py, tests/test_ui.py", body)
        self.assertIn("parallel-eligible: false", body)
        self.assertIn("Epic: #100", body)
        self.assertEqual(
            triage_backlog.ready_gaps(
                {
                    "number": 999,
                    "body": body,
                    "labels": [{"name": "type:feat"}],
                },
                set(),
                repo_slug="owner/repo",
            ),
            [],
        )

    def test_cycle_refusal_names_the_offending_edge(self):
        manifest = self.manifest()
        manifest["issues"][0]["depends_on"] = ["interface"]

        with self.assertRaisesRegex(pti.PlanError, r"cycle detected at edge \w+ -> \w+"):
            pti.prepare_plan(manifest, self.repo, self.inventory)

    def test_overlap_detection_derives_parallel_eligibility(self):
        manifest = self.manifest()
        manifest["issues"][1]["depends_on"] = []
        manifest["issues"][1]["change_targets"] = [
            {"path": "src", "kind": "existing"}
        ]
        manifest["issues"][0]["change_targets"] = [
            {"path": "src/core.py", "kind": "existing"}
        ]

        plan = pti.prepare_plan(manifest, self.repo, self.inventory)

        self.assertEqual(
            plan.issues[1].touches,
            ("src/core.py", "src/nested/deep.py", "src/ui.py"),
        )
        self.assertFalse(plan.issues[0].parallel_eligible)
        self.assertFalse(plan.issues[1].parallel_eligible)

    def test_repository_footprints_validate_existing_glob_and_new_parent(self):
        touches = pti.derive_touches(
            [
                {"path": "src/*.py", "kind": "existing"},
                {"path": "tests/test_new.py", "kind": "new"},
            ],
            self.repo,
            self.inventory,
            issue_key="new-slice",
        )
        self.assertEqual(
            touches,
            ("src/core.py", "src/nested/deep.py", "src/ui.py", "tests/test_new.py"),
        )

        with self.assertRaisesRegex(pti.PlanError, "parent directory is not in the repository"):
            pti.derive_touches(
                [{"path": "unknown/new.py", "kind": "new"}],
                self.repo,
                self.inventory,
                issue_key="bad-slice",
            )
        with self.assertRaisesRegex(pti.PlanError, "safe repository-relative path"):
            pti.derive_touches(
                [{"path": "/tmp/escape.py", "kind": "new"}],
                self.repo,
                self.inventory,
                issue_key="escape",
            )

    def test_new_file_accepts_missing_intermediate_directories_under_known_ancestor(self):
        touches = pti.derive_touches(
            [{"path": "src/auth/session/login.py", "kind": "new"}],
            self.repo,
            self.inventory,
            issue_key="nested-new-slice",
        )

        self.assertEqual(touches, ("src/auth/session/login.py",))

    def test_new_file_rejects_existing_directory(self):
        with self.assertRaisesRegex(pti.PlanError, "existing directory"):
            pti.derive_touches(
                [{"path": "src/nested", "kind": "new"}],
                self.repo,
                self.inventory,
                issue_key="directory-collision",
            )

    def test_intake_contract_rejects_type_title_and_verify_placeholders(self):
        manifest = self.manifest()
        manifest["issues"][0]["type"] = "test"
        with self.assertRaisesRegex(pti.PlanError, "unsupported type"):
            pti.prepare_plan(manifest, self.repo, self.inventory)

        manifest = self.manifest()
        manifest["issues"][0]["title"] = "fix: wrong prefix"
        with self.assertRaisesRegex(pti.PlanError, "title must start with 'feat:'"):
            pti.prepare_plan(manifest, self.repo, self.inventory)

        manifest = self.manifest()
        manifest["issues"][0]["acceptance_criteria"][0]["verify"] = "TODO"
        with self.assertRaisesRegex(pti.PlanError, "verify is a placeholder"):
            pti.prepare_plan(manifest, self.repo, self.inventory)

        manifest = self.manifest()
        manifest["issues"][0]["summary"] = "Looks valid.\ntouches: **"
        with self.assertRaisesRegex(pti.PlanError, "summary must be a single line"):
            pti.prepare_plan(manifest, self.repo, self.inventory)

    @patch.object(pti, "validate_source_prd")
    @patch.object(pti, "validate_publication_labels")
    @patch.object(pti, "_attach_to_board")
    @patch.object(pti, "_create_issue", side_effect=[100, 101, 102])
    def test_publish_creates_epics_then_dependency_order_and_attaches_every_item(
        self, create, attach, validate_labels, validate_source
    ):
        plan = pti.prepare_plan(self.manifest(), self.repo, self.inventory)

        created = pti.publish_plan(plan, self.repo, "owner/repo")

        self.assertEqual(created, {"phase-one": 100, "foundation": 101, "interface": 102})
        validate_source.assert_called_once_with(82, "owner/repo")
        validate_labels.assert_called_once_with(plan, "owner/repo")
        self.assertEqual(attach.call_args_list, [
            call(100, self.repo), call(101, self.repo), call(102, self.repo)
        ])
        interface_body = create.call_args_list[2].args[2]
        self.assertIn("depends-on: #101", interface_body)
        self.assertIn("Epic: #100", interface_body)

    @patch.object(pti, "validate_source_prd")
    @patch.object(pti, "validate_publication_labels")
    @patch.object(pti, "_attach_to_board", side_effect=pti.PublicationError("board unavailable"))
    @patch.object(pti, "_create_issue", return_value=100)
    def test_publication_stops_and_reports_created_issue_on_board_failure(
        self, _create, _attach, _labels, _source
    ):
        plan = pti.prepare_plan(self.manifest(), self.repo, self.inventory)

        with self.assertRaisesRegex(
            pti.PublicationError, r"board unavailable; created before stop: phase-one=#100"
        ):
            pti.publish_plan(plan, self.repo, "owner/repo")

    @patch.object(pti, "run_cmd")
    def test_source_prd_must_be_open_approved_and_planning_ready(self, run_cmd):
        run_cmd.return_value = (
            0,
            json.dumps({
                "state": "OPEN",
                "labels": [{"name": "type:epic"}],
                "body": "- Readiness: `READY_FOR_PLANNING`\n- Operator approval: `APPROVED`",
            }),
            "",
        )
        pti.validate_source_prd(82, "owner/repo")

        run_cmd.return_value = (
            0,
            json.dumps({
                "state": "OPEN",
                "labels": [{"name": "type:epic"}],
                "body": "- Readiness: `BLOCKED`\n- Operator approval: `APPROVED`",
            }),
            "",
        )
        with self.assertRaisesRegex(pti.PublicationError, "not READY_FOR_PLANNING"):
            pti.validate_source_prd(82, "owner/repo")

    @patch.object(pti, "run_cmd")
    def test_publication_preflight_refuses_missing_governance_labels(self, run_cmd):
        plan = pti.prepare_plan(self.manifest(), self.repo, self.inventory)
        run_cmd.return_value = (0, json.dumps([{"name": "type:epic"}]), "")

        with self.assertRaisesRegex(
            pti.PublicationError, "required governance labels are missing"
        ):
            pti.validate_publication_labels(plan, "owner/repo")

    @patch.object(pti, "publish_plan")
    @patch.object(pti, "get_repo_slug")
    @patch.object(pti, "resolve_repo_root")
    @patch.object(pti, "repository_inventory")
    def test_dry_run_performs_no_github_publication(
        self, inventory, resolve, repo_slug, publish
    ):
        resolve.return_value = self.repo
        inventory.return_value = self.inventory
        plan_file = self.repo / "plan.json"
        plan_file.write_text(json.dumps(self.manifest()))

        with patch.object(
            sys, "argv", ["prd_to_issues.py", "--plan", str(plan_file), "--dry-run"]
        ):
            result = pti.main()

        self.assertEqual(result, 0)
        repo_slug.assert_not_called()
        publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
