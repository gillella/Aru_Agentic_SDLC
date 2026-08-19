import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import triage_backlog as tb
import fetch_next_issue  # noqa: E402


def issue(number, *labels, body="", title="t", author="owner"):
    record = {
        "number": number,
        "title": title,
        "body": body,
        "labels": [{"name": name} for name in labels],
    }
    if author is not None:
        record["author"] = {"login": author}
    return record


READY_BODY = """## Summary

Do the thing.

## Acceptance Criteria

- [ ] it works
- [ ] it is documented

## Verification

`pytest -q` exits 0.

## Dependencies

depends-on:
touches: src/thing.py, tests/test_thing.py
parallel-eligible: true
"""


class TrustedOwnerTests(unittest.TestCase):
    def setUp(self):
        owner = patch.object(
            fetch_next_issue, "repository_owner_login", return_value="owner")
        trusted = patch.object(
            fetch_next_issue, "repository_trusted_logins", return_value={"owner"})
        self.addCleanup(owner.stop)
        self.addCleanup(trusted.stop)
        owner.start()
        trusted.start()


class SectionParsingTests(unittest.TestCase):
    def test_reads_criteria_under_the_heading(self):
        self.assertEqual(len(tb.acceptance_criteria(READY_BODY)), 2)

    def test_reads_criteria_under_bug_heading_aliases(self):
        bug_body = "## Acceptance Criteria / Expected Behavior\n\n- [ ] Predicate 1 (verify: `test`)\n\n## Verification\n\nx\n"
        self.assertEqual(len(tb.acceptance_criteria(bug_body)), 1)
        pred_body = "## Expected Behavior / Predicates\n\n- [ ] Predicate 1 (verify: `test`)\n\n## Verification\n\nx\n"
        self.assertEqual(len(tb.acceptance_criteria(pred_body)), 1)

    def test_checkboxes_without_the_heading_do_not_count(self):
        # A stray task list is not acceptance criteria; counting it would
        # promote under-specified issues.
        self.assertEqual(tb.acceptance_criteria("## Notes\n\n- [ ] something\n"), [])

    def test_has_machine_checkable_predicates(self):
        self.assertFalse(tb.has_machine_checkable_predicates(["- [ ] it works", "- [ ] it is done"]))
        self.assertFalse(tb.has_machine_checkable_predicates(["- [ ] Assert the UI looks good"]))
        self.assertFalse(tb.has_machine_checkable_predicates(["- [ ] Document the `result` field"]))
        self.assertFalse(tb.has_machine_checkable_predicates(["- [ ] Predicate 1 (verify: `command to verify`)"]))
        self.assertFalse(tb.has_machine_checkable_predicates(["- [ ] Predicate 1 (verify: command to verify)"]))
        self.assertFalse(tb.has_machine_checkable_predicates(["- [ ] Predicate 1 (verify: `TODO`)"]))
        self.assertFalse(tb.has_machine_checkable_predicates(["- [ ] Predicate 1 (verify: `<command>`)"]))
        self.assertFalse(tb.has_machine_checkable_predicates(["- [ ] Predicate 1 (verify: `...`)"]))
        self.assertFalse(tb.has_machine_checkable_predicates(["- [ ] Predicate 1 (verify: none)"]))
        self.assertFalse(tb.has_machine_checkable_predicates(["- [ ] Predicate 1 (verify: manually click through the app)"]))
        self.assertFalse(tb.has_machine_checkable_predicates(["- [ ] Predicate 1 (verify: ensure the button looks nice)"]))
        self.assertFalse(tb.has_machine_checkable_predicates(["- [ ] Predicate 1 (verify: review the rendered page)"]))
        self.assertFalse(tb.has_machine_checkable_predicates(["- [ ] Predicate 1 (verify: confirm expected behavior)"]))
        self.assertFalse(tb.has_machine_checkable_predicates(["- [ ] Predicate 1 (verify: read the generated report)"]))
        self.assertFalse(tb.has_machine_checkable_predicates(["- [ ] Valid (verify: `pytest -q`)", "- [ ] Vague predicate"]))
        self.assertTrue(tb.has_machine_checkable_predicates(["- [ ] Predicate 1 (verify: `pytest -q`)"]))
        self.assertTrue(tb.has_machine_checkable_predicates(["- [ ] asserts returncode is 0"]))
        self.assertTrue(tb.has_machine_checkable_predicates(["- [ ] check `python3 scripts/merge_pr.py` exits 0"]))
        self.assertTrue(tb.has_machine_checkable_predicates(["- [ ] Predicate 1 (verify: manual - docs update)", "- [ ] Predicate 2 [manual: no runtime change]"]))

    def test_verification_section_detected(self):
        self.assertTrue(tb.has_verification(READY_BODY))

    def test_empty_verification_section_is_not_verification(self):
        self.assertFalse(tb.has_verification("## Verification\n\n## Dependencies\n\nx\n"))

    def test_placeholder_decision_boundaries_rejected(self):
        self.assertFalse(tb.has_decision_boundaries("## Decision Boundaries\n<!-- comment -->\n- Default:\n- Edge cases:\n- Error handling:\n\n## Next"))
        self.assertFalse(tb.has_decision_boundaries("## Decision Boundaries\n\n## Next"))
        self.assertTrue(tb.has_decision_boundaries("## Decision Boundaries\n- Default: fallback value\n- Edge cases: none\n\n## Next"))

    def test_placeholder_non_goals_rejected(self):
        self.assertFalse(tb.has_non_goals("## Non-Goals\n<!-- comment -->\n- \n\n## Next"))
        self.assertFalse(tb.has_non_goals("## Non-Goals\n\n## Next"))
        self.assertTrue(tb.has_non_goals("## Non-Goals\n- Do not rewrite auth engine\n\n## Next"))


CONFORMING_FEAT_BODY = """## Summary

Do the feature.

## Acceptance Criteria

- [ ] Predicate 1 (verify: `pytest -q`)
- [ ] Predicate 2 (verify: `python3 scripts/foo.py --check`)

## Decision Boundaries
- Default: value
- Edge cases: handling

## Non-Goals
- Out of scope

## Verification

`pytest -q` exits 0.

## Dependencies

depends-on:
touches: src/thing.py, tests/test_thing.py
parallel-eligible: true
"""


class ReadyContractTests(unittest.TestCase):
    def test_complete_issue_has_no_gaps(self):
        self.assertEqual(tb.ready_gaps(issue(10, "type:chore", body=READY_BODY), set()), [])

    def test_conforming_feat_issue_has_no_gaps(self):
        self.assertEqual(tb.ready_gaps(issue(200, "type:feat", body=CONFORMING_FEAT_BODY), set()), [])

    def test_new_feat_issue_missing_decision_boundaries_and_non_goals_is_blocked(self):
        gaps = tb.ready_gaps(issue(200, "type:feat", body=READY_BODY), set())
        self.assertIn("missing section: ## Decision Boundaries", gaps)
        self.assertIn("missing section: ## Non-Goals", gaps)

    def test_new_feat_issue_with_untouched_placeholders_is_blocked(self):
        body = READY_BODY + "\n## Decision Boundaries\n- Default:\n- Edge cases:\n\n## Non-Goals\n- \n"
        gaps = tb.ready_gaps(issue(200, "type:feat", body=body), set())
        self.assertIn("missing section: ## Decision Boundaries", gaps)
        self.assertIn("missing section: ## Non-Goals", gaps)

    def test_legacy_issue_in_aru_sdlc_repo_warns_and_passes(self):
        with patch("triage_backlog.get_repo_slug", return_value="gillella/Aru_Agentic_SDLC"), patch("sys.stderr.write") as mock_stderr:
            gaps = tb.ready_gaps(issue(100, "type:feat", body=READY_BODY), set(), repo_slug="gillella/Aru_Agentic_SDLC")
            self.assertEqual(gaps, [])  # Passes for legacy issue <= 158 in Aru_Agentic_SDLC
            written = "".join(call.args[0] for call in mock_stderr.call_args_list)
            self.assertIn("[WARN] Pre-existing legacy issue #100", written)

    def test_legacy_number_in_downstream_repo_is_not_exempt(self):
        gaps = tb.ready_gaps(issue(10, "type:feat", body=READY_BODY), set(), repo_slug="acme/my-service")
        self.assertIn("missing section: ## Decision Boundaries", gaps)
        self.assertIn("missing section: ## Non-Goals", gaps)

    def test_missing_touches_is_reported(self):
        body = READY_BODY.replace("touches: src/thing.py, tests/test_thing.py", "touches:")
        gaps = tb.ready_gaps(issue(10, "type:chore", body=body), set())
        self.assertIn("no touches: declaration", gaps)

    def test_missing_criteria_and_verification_reported_together(self):
        gaps = tb.ready_gaps(issue(10, "type:chore", body="## Summary\n\nx\n"), set())
        self.assertIn("no acceptance criteria checkboxes", gaps)
        self.assertIn("no verification section", gaps)

    def test_open_dependency_blocks(self):
        body = READY_BODY.replace("depends-on:", "depends-on: #5, #6")
        gaps = tb.ready_gaps(issue(10, "type:chore", body=body), {5})
        self.assertTrue(any("depends-on still open" in g and "#5" in g for g in gaps))

    def test_closed_dependency_does_not_block(self):
        body = READY_BODY.replace("depends-on:", "depends-on: #5")
        self.assertEqual(tb.ready_gaps(issue(10, "type:chore", body=body), set()), [])

    def test_new_feat_issue_with_vague_criteria_is_blocked(self):
        vague_feat_body = CONFORMING_FEAT_BODY.replace(
            "- [ ] Predicate 1 (verify: `pytest -q`)",
            "- [ ] it works",
        ).replace("- [ ] Predicate 2", "- [ ] it is finished")
        gaps = tb.ready_gaps(issue(200, "type:feat", body=vague_feat_body), set())
        self.assertIn("acceptance criteria lack machine-checkable predicate (e.g., '(verify: `cmd`)' or test assertion)", gaps)

    def test_bug_report_template_filled_passes_with_zero_gaps(self):
        bug_body = """## Problem Description
A bug occurred.

## Acceptance Criteria / Expected Behavior
- [ ] Returns exit code 0 when valid (verify: `python3 -m unittest tests/test_foo.py`)

## Decision Boundaries
- Default: fallback
- Error handling: raise ValueError on invalid input

## Non-Goals
- Performance optimizations

## Steps to Reproduce
1. Run command

## Verification
`python3 -m unittest tests/test_foo.py` exits 0

## Dependencies
depends-on: none
touches: scripts/foo.py
parallel-eligible: true
"""
        gaps = tb.ready_gaps(issue(200, "type:fix", body=bug_body), set())
        self.assertEqual(gaps, [])

    def test_machine_checkable_predicates_rejects_bare_inline_code(self):
        bare_code_criteria = [
            "- [ ] Document the `result` field in the response",
            "- [ ] Update `my_var` variable in `README.md`",
        ]
        self.assertFalse(tb.has_machine_checkable_predicates(bare_code_criteria))

    def test_machine_checkable_predicates_accepts_verify_and_assertions(self):
        verify_criteria = ["- [ ] Verify output (verify: `python3 test.py`)"]
        self.assertTrue(tb.has_machine_checkable_predicates(verify_criteria))

        assert_criteria = ["- [ ] Asserts that return code is 0"]
        self.assertTrue(tb.has_machine_checkable_predicates(assert_criteria))

        exit_criteria = ["- [ ] Exits with code 0 on valid input"]
        self.assertTrue(tb.has_machine_checkable_predicates(exit_criteria))

    def test_feature_request_template_with_untouched_criteria_placeholder_is_blocked(self):
        tmpl = (Path(__file__).resolve().parents[1] / ".github" / "ISSUE_TEMPLATE" / "feature_request.md").read_text()
        body = tmpl + "\n## Decision Boundaries\n- Default: 0\n\n## Non-Goals\n- None\n\n## Verification\n`pytest` exits 0\n\n## Dependencies\ntouches: scripts/foo.py\n"
        gaps = tb.ready_gaps(issue(200, "type:feat", body=body), set())
        self.assertIn("acceptance criteria lack machine-checkable predicate (e.g., '(verify: `cmd`)' or test assertion)", gaps)

    def test_bug_report_template_with_untouched_criteria_placeholder_is_blocked(self):
        tmpl = (Path(__file__).resolve().parents[1] / ".github" / "ISSUE_TEMPLATE" / "bug_report.md").read_text()
        body = tmpl + "\n## Decision Boundaries\n- Default: 0\n\n## Non-Goals\n- None\n\n## Verification\n`pytest` exits 0\n\n## Dependencies\ntouches: scripts/foo.py\n"
        gaps = tb.ready_gaps(issue(200, "type:fix", body=body), set())
        self.assertIn("acceptance criteria lack machine-checkable predicate (e.g., '(verify: `cmd`)' or test assertion)", gaps)

    def test_example_conforming_issue_body_passes_ready_contract(self):
        gaps = tb.ready_gaps(issue(200, "type:feat", body=tb.EXAMPLE_CONFORMING_ISSUE_BODY), set())
        self.assertEqual(gaps, [])

    def test_epic_is_never_ready(self):
        gaps = tb.ready_gaps(issue(2, "type:epic", body=READY_BODY), set())
        self.assertEqual(len(gaps), 1)
        self.assertIn("epic", gaps[0])

    def test_needs_human_issue_is_never_promoted(self):
        operator_issue = issue(
            14,
            "type:chore",
            "status:backlog",
            "needs-human",
            body=READY_BODY,
        )
        gaps = tb.ready_gaps(operator_issue, set())
        self.assertEqual(
            gaps,
            ["needs-human (operator-only; factory agents must not claim)"],
        )

        for force in (False, True):
            argv = ["triage_backlog.py", "--promote"]
            if force:
                argv.append("--force")
            with patch("triage_backlog.list_open_issues", return_value=[operator_issue]), \
                 patch("triage_backlog.list_open_pr_files_by_issue", return_value={}), \
                 patch("triage_backlog.update_status") as update, \
                 patch("sys.argv", argv):
                self.assertEqual(tb.main(), 0)
            update.assert_not_called()

    def test_main_refusal_emits_example_conforming_issue(self):
        issues_list = [issue(200, "type:feat", "status:backlog", body=READY_BODY)]
        with patch("triage_backlog.list_open_issues", return_value=issues_list), \
             patch("triage_backlog.list_open_pr_files_by_issue", return_value={}), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_stdout, \
             patch("sys.argv", ["triage_backlog.py"]):
            tb.main()
            output = mock_stdout.getvalue()
            self.assertIn("Example of a conforming issue with machine-checkable criteria:", output)
            self.assertIn("## Decision Boundaries", output)
            self.assertIn("## Non-Goals", output)
            self.assertIn("## Dependencies", output)


class SplitRecommendationTests(unittest.TestCase):
    @staticmethod
    def oversized_body():
        criteria = "\n".join(f"- [ ] predicate {n}" for n in range(1, 10))
        return READY_BODY.replace(
            "- [ ] it works\n- [ ] it is documented",
            criteria,
        ).replace(
            "touches: src/thing.py, tests/test_thing.py",
            "touches: scripts/thing.py, hooks/guard.py, tests/test_thing.py",
        )

    def test_narrow_issue_promotes(self):
        narrow_body = READY_BODY.replace(
            "touches: src/thing.py, tests/test_thing.py",
            "touches: src/thing.py, src/thing_test.py",
        )
        narrow = issue(10, "type:chore", "status:backlog", body=narrow_body)
        with patch("triage_backlog.list_open_issues", return_value=[narrow]), \
             patch("triage_backlog.list_open_pr_files_by_issue", return_value={}), \
             patch("triage_backlog.update_status", return_value=True) as update, \
             patch("sys.argv", ["triage_backlog.py", "--promote"]):
            self.assertEqual(tb.main(), 0)
        update.assert_called_once_with(10, "Ready")

    def test_wide_touches_plus_many_criteria_is_held_for_split(self):
        wide = issue(11, "type:chore", "status:backlog", body=self.oversized_body())
        with patch("triage_backlog.list_open_issues", return_value=[wide]), \
             patch("triage_backlog.list_open_pr_files_by_issue", return_value={}), \
             patch("triage_backlog.update_status") as update, \
             patch("sys.argv", ["triage_backlog.py", "--promote"]), \
             patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(tb.main(), 0)
        update.assert_not_called()
        self.assertIn("SPLIT", output.getvalue())
        self.assertIn("not a human gate", output.getvalue())
        self.assertIn("9 acceptance criteria exceed the threshold of 8", output.getvalue())
        # tests/ is a companion area, so the span is hooks+scripts. The
        # accompanying test file never widens scope on its own.
        self.assertIn("2 top-level areas: hooks, scripts", output.getvalue())

    def test_each_oversize_signal_is_independently_actionable(self):
        wide_only = READY_BODY.replace(
            "touches: src/thing.py, tests/test_thing.py",
            "touches: scripts/thing.py, hooks/guard.py",
        )
        many_only = self.oversized_body().replace(
            "touches: scripts/thing.py, hooks/guard.py, tests/test_thing.py",
            "touches: scripts/thing.py, scripts/thing_test.py",
        )

        self.assertEqual(
            tb.split_reasons(issue(20, "type:chore", body=wide_only)),
            ["touches span 2 top-level areas: hooks, scripts"],
        )
        self.assertEqual(
            tb.split_reasons(issue(21, "type:chore", body=many_only)),
            ["9 acceptance criteria exceed the threshold of 8"],
        )

    def test_wildcard_top_level_roots_are_inherently_wide(self):
        repo_wide = READY_BODY.replace(
            "touches: src/thing.py, tests/test_thing.py",
            "touches: **/*.py",
        )
        per_area = READY_BODY.replace(
            "touches: src/thing.py, tests/test_thing.py",
            "touches: */config.yml",
        )

        self.assertEqual(
            tb.split_reasons(issue(22, "type:chore", body=repo_wide)),
            ["touches use wildcard top-level area patterns: **"],
        )
        self.assertEqual(
            tb.split_reasons(issue(23, "type:chore", body=per_area)),
            ["touches use wildcard top-level area patterns: *"],
        )
        for declaration in ("scripts*", "[st]rc", "?", "*.py"):
            with self.subTest(declaration=declaration):
                body = READY_BODY.replace(
                    "touches: src/thing.py, tests/test_thing.py",
                    f"touches: {declaration}",
                )
                self.assertEqual(
                    tb.split_reasons(issue(28, "type:chore", body=body)),
                    [f"touches use wildcard top-level area patterns: {declaration}"],
                )

    def test_directory_spelling_preserves_top_level_areas(self):
        for declaration in (
            "scripts/, hooks/",
            "scripts, hooks",
            ".github, .devcontainer",
            ".github, README.md",
        ):
            with self.subTest(declaration=declaration):
                body = READY_BODY.replace(
                    "touches: src/thing.py, tests/test_thing.py",
                    f"touches: {declaration}",
                )
                reasons = tb.split_reasons(issue(29, "type:chore", body=body))
                self.assertEqual(len(reasons), 1)
                self.assertIn("touches span 2 top-level areas", reasons[0])

    def test_promote_holds_directory_and_slash_free_wildcard_scopes(self):
        bodies = (
            READY_BODY.replace(
                "touches: src/thing.py, tests/test_thing.py",
                "touches: scripts/, hooks/",
            ),
            READY_BODY.replace(
                "touches: src/thing.py, tests/test_thing.py",
                "touches: scripts, hooks",
            ),
            READY_BODY.replace(
                "touches: src/thing.py, tests/test_thing.py",
                "touches: .github, .devcontainer",
            ),
            READY_BODY.replace(
                "touches: src/thing.py, tests/test_thing.py",
                "touches: .github, README.md",
            ),
            READY_BODY.replace(
                "touches: src/thing.py, tests/test_thing.py",
                "touches: scripts*",
            ),
        )
        for number, body in enumerate(bodies, start=30):
            candidate = issue(number, "type:chore", "status:backlog", body=body)
            with self.subTest(number=number), \
                 patch("triage_backlog.list_open_issues", return_value=[candidate]), \
                 patch("triage_backlog.list_open_pr_files_by_issue", return_value={}), \
                 patch("triage_backlog.update_status") as update, \
                 patch("sys.argv", ["triage_backlog.py", "--promote"]), \
                 patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(tb.main(), 0)
                update.assert_not_called()

    def test_leading_dot_slash_is_normalized_before_area_classification(self):
        body = READY_BODY.replace(
            "touches: src/thing.py, tests/test_thing.py",
            "touches: ./scripts/a.py, ./hooks/b.py",
        )
        self.assertEqual(
            tb.split_reasons(issue(24, "type:chore", body=body)),
            ["touches span 2 top-level areas: hooks, scripts"],
        )

    def test_explicit_repository_root_is_inherently_wide(self):
        for declaration in (".", "./", "/"):
            with self.subTest(declaration=declaration):
                body = READY_BODY.replace(
                    "touches: src/thing.py, tests/test_thing.py",
                    f"touches: {declaration}",
                )
                self.assertEqual(
                    tb.split_reasons(issue(25, "type:chore", body=body)),
                    ["touches include the whole repository root"],
                )

    def test_root_files_share_one_area_but_conflict_with_a_directory_area(self):
        root_files = READY_BODY.replace(
            "touches: src/thing.py, tests/test_thing.py",
            "touches: README.md, pyproject.toml",
        )
        root_and_scripts = READY_BODY.replace(
            "touches: src/thing.py, tests/test_thing.py",
            "touches: README.md, scripts/tool.py",
        )
        self.assertEqual(
            tb.split_reasons(issue(26, "type:chore", body=root_files)), []
        )
        self.assertEqual(
            tb.split_reasons(issue(27, "type:chore", body=root_and_scripts)),
            ["touches span 2 top-level areas: <root>, scripts"],
        )

    def test_bare_names_use_repository_evidence_and_unknowns_are_conservative(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "LICENSE").write_text("license", encoding="utf-8")
            (root / "Makefile").write_text("all:", encoding="utf-8")
            (root / ".github").mkdir()
            root_files = READY_BODY.replace(
                "touches: src/thing.py, tests/test_thing.py",
                "touches: LICENSE, Makefile",
            )
            dotted_dir = READY_BODY.replace(
                "touches: src/thing.py, tests/test_thing.py",
                "touches: .github, LICENSE",
            )
            unknowns = READY_BODY.replace(
                "touches: src/thing.py, tests/test_thing.py",
                "touches: FutureDir, AnotherDir",
            )
            with patch("triage_backlog._repository_root", return_value=tmp):
                self.assertEqual(
                    tb.split_reasons(issue(32, "type:chore", body=root_files)), []
                )
                self.assertEqual(
                    tb.split_reasons(issue(33, "type:chore", body=dotted_dir)),
                    ["touches span 2 top-level areas: .github, <root>"],
                )
                self.assertEqual(
                    tb.split_reasons(issue(34, "type:chore", body=unknowns)),
                    ["touches span 2 top-level areas: AnotherDir, FutureDir"],
                )

    def test_force_promotes_split_recommended_issue(self):
        wide = issue(12, "type:chore", "status:backlog", body=self.oversized_body())
        with patch("triage_backlog.list_open_issues", return_value=[wide]), \
             patch("triage_backlog.list_open_pr_files_by_issue", return_value={}), \
             patch("triage_backlog.update_status", return_value=True) as update, \
             patch("sys.argv", ["triage_backlog.py", "--promote", "--force"]):
            self.assertEqual(tb.main(), 0)
        update.assert_called_once_with(12, "Ready")

    def test_force_does_not_promote_epic(self):
        epic = issue(13, "type:epic", "status:backlog", body=self.oversized_body())
        with patch("triage_backlog.list_open_issues", return_value=[epic]), \
             patch("triage_backlog.list_open_pr_files_by_issue", return_value={}), \
             patch("triage_backlog.update_status") as update, \
             patch("sys.argv", ["triage_backlog.py", "--promote", "--force"]):
            self.assertEqual(tb.main(), 0)
        update.assert_not_called()

    def test_companion_areas_do_not_widen_scope(self):
        """tests/ and docs/ accompany production work instead of widening it."""
        for declaration in (
            "scripts/thing.py, tests/test_thing.py",
            "src/thing.py, tests/test_thing.py",
            "scripts/thing.py, tests/test_thing.py, docs/guide.md",
            "tests/test_thing.py, docs/guide.md",
            "docs/guide.md",
        ):
            with self.subTest(declaration=declaration):
                body = READY_BODY.replace(
                    "touches: src/thing.py, tests/test_thing.py",
                    f"touches: {declaration}",
                )
                self.assertEqual(
                    tb.split_reasons(issue(40, "type:chore", body=body)), []
                )

    def test_companion_areas_do_not_mask_real_scope_creep(self):
        """Two production areas still split even when tests/ rides along."""
        for declaration, expected in (
            ("scripts/a.py, src/b.py, tests/test_a.py",
             "touches span 2 top-level areas: scripts, src"),
            ("scripts/a.py, hooks/g.sh, tests/test_a.py, docs/d.md",
             "touches span 2 top-level areas: hooks, scripts"),
        ):
            with self.subTest(declaration=declaration):
                body = READY_BODY.replace(
                    "touches: src/thing.py, tests/test_thing.py",
                    f"touches: {declaration}",
                )
                self.assertEqual(
                    tb.split_reasons(issue(41, "type:chore", body=body)),
                    [expected],
                )

    def test_the_documented_example_issue_is_promotable(self):
        """Regression guard for the triage/merge deadlock (#288).

        merge_pr.py requires a tests/ change whenever src/ or scripts/ changes.
        When tests/ also counted as a second top-level area, the only shape that
        cleared this module was guaranteed to fail the merge gate - and the
        example body printed here as the conforming template was itself held.
        Following our own documented instructions must yield a promotable issue.
        """
        self.assertEqual(
            tb.split_reasons({"body": tb.EXAMPLE_CONFORMING_ISSUE_BODY,
                              "labels": []}),
            [],
        )


class ReadyDocstringContractTests(unittest.TestCase):
    """The module docstring must describe the contract ready_gaps() enforces.

    #288 regression guard: the docstring once claimed "all four required"
    while ready_gaps() enforced nine elements, so anyone filing from the
    docstring wrote an issue that failed triage.
    """

    def test_docstring_does_not_claim_four_required(self):
        doc = tb.__doc__
        self.assertNotIn("all four", doc)
        self.assertNotIn("four required", doc)

    def test_docstring_names_every_enforced_element(self):
        doc = tb.__doc__
        for element in (
            "needs-human", "epic", "acceptance criteria", "verification",
            "touches:", "Decision Boundaries", "Non-Goals",
        ):
            with self.subTest(element=element):
                self.assertIn(element, doc)

    def test_docstring_contract_accepts_a_conforming_issue(self):
        # The docstring's own claims must be consistent with ready_gaps: an
        # issue that meets every named element is promotable.
        self.assertEqual(
            tb.ready_gaps({"body": tb.EXAMPLE_CONFORMING_ISSUE_BODY,
                           "labels": []}, set()),
            [],
        )


class PartitionTests(TrustedOwnerTests):
    def test_splits_by_status_and_claim(self):
        issues = [
            issue(1, "status:backlog"),
            issue(2, "status:ready"),
            issue(3, "status:in-progress", "agent:agent-1"),
        ]
        backlog, ready, held = tb.partition(issues)
        self.assertEqual([i["number"] for i in backlog], [1])
        self.assertEqual([i["number"] for i in ready], [2])
        self.assertEqual([i["number"] for i in held], [3])

    def test_a_claimed_ready_issue_counts_as_held_not_ready(self):
        issues = [issue(4, "status:ready", "agent:agent-2")]
        _backlog, ready, held = tb.partition(issues)
        self.assertEqual(ready, [])
        self.assertEqual([i["number"] for i in held], [4])

    def test_ready_needs_human_issue_is_backlog_and_not_capacity(self):
        operator_issue = issue(
            7,
            "status:ready",
            "needs-human",
            body=READY_BODY,
        )

        backlog, ready, held = tb.partition([operator_issue])

        self.assertEqual([item["number"] for item in backlog], [7])
        self.assertEqual(ready, [])
        self.assertEqual(held, [])
        self.assertEqual(tb.capacity([operator_issue], []), {
            "concurrent": [],
            "deferred": [],
            "ready_total": 0,
        })

        with patch("triage_backlog.list_open_issues", return_value=[operator_issue]), \
             patch("triage_backlog.list_open_pr_files_by_issue", return_value={}), \
             patch("sys.stdout", new_callable=io.StringIO) as output, \
             patch("sys.argv", ["triage_backlog.py", "--capacity"]):
            self.assertEqual(tb.main(), 0)
        self.assertIn("Ready issues:            0", output.getvalue())
        self.assertIn("Claimable simultaneously: 0", output.getvalue())

    def test_active_needs_human_touches_block_overlapping_capacity(self):
        operator_issue = issue(
            7,
            "status:in-progress",
            "agent:human",
            "needs-human",
            body=READY_BODY.replace(
                "touches: src/thing.py, tests/test_thing.py",
                "touches: src/operator.py",
            ),
        )
        factory_issue = issue(
            8,
            "status:ready",
            body=READY_BODY.replace(
                "touches: src/thing.py, tests/test_thing.py",
                "touches: src/operator.py",
            ),
        )

        backlog, ready, held = tb.partition([operator_issue, factory_issue])
        cap = tb.capacity(ready, held)

        self.assertEqual(backlog, [])
        self.assertEqual([item["number"] for item in held], [7])
        self.assertEqual([item["number"] for item in ready], [8])
        self.assertEqual(cap["concurrent"], [])
        self.assertEqual(cap["deferred"], [(8, "path conflict on src/operator.py")])

    def test_in_review_issue_is_parked_and_releases_path_reservation(self):
        issues = [
            issue(39, "status:in-review", "agent:agent-1",
                  body="touches: src/a.py"),
            issue(40, "status:ready", body="touches: src/a.py"),
        ]

        _backlog, ready, held = tb.partition(issues)
        cap = tb.capacity(ready, held)

        self.assertEqual([i["number"] for i in held], [39])
        self.assertEqual(cap["concurrent"], [40])
        self.assertEqual(cap["deferred"], [])

    def test_in_review_pr_files_cannot_restore_released_reservation(self):
        issues = [
            issue(39, "status:in-review", "agent:agent-1",
                  body="touches: src/a.py, src/unused.py"),
            issue(40, "status:ready", body="touches: src/unused.py"),
        ]

        _backlog, ready, held = tb.partition(issues)
        cap = tb.capacity(ready, held, pr_files_by_issue={39: ["src/unused.py"]})

        self.assertEqual(cap["concurrent"], [40])
        self.assertEqual(cap["deferred"], [])

    def test_standard_triage_report_ignores_in_review_pr_file_mapping(self):
        held = issue(39, "status:in-review", "agent:agent-1",
                     body="touches: src/a.py, src/unused.py")
        ready = issue(40, "status:ready", body="touches: src/unused.py")
        with patch("triage_backlog.list_open_issues", return_value=[held, ready]), \
             patch("triage_backlog.list_open_pr_files_by_issue",
                   return_value={39: ["src/unused.py"]}), \
             patch("sys.stdout", new_callable=io.StringIO) as output, \
             patch("sys.argv", ["triage_backlog.py"]):
            self.assertEqual(tb.main(), 0)
        self.assertIn("Claimable simultaneously: 1  [40]", output.getvalue())


class CapacityTests(TrustedOwnerTests):
    def test_non_overlapping_issues_are_all_concurrent(self):
        ready = [
            issue(1, "status:ready", body="touches: src/a.py"),
            issue(2, "status:ready", body="touches: src/b.py"),
        ]
        cap = tb.capacity(ready, [])
        self.assertEqual(cap["concurrent"], [1, 2])

    def test_overlapping_issues_are_deferred(self):
        ready = [
            issue(1, "status:ready", body="touches: src/a.py"),
            issue(2, "status:ready", body="touches: src/a.py"),
        ]
        cap = tb.capacity(ready, [])
        self.assertEqual(cap["concurrent"], [1])
        self.assertEqual([n for n, _ in cap["deferred"]], [2])

    def test_conflict_with_in_flight_work_defers(self):
        held = [issue(9, "status:in-progress", "agent:a", body="touches: src/a.py")]
        ready = [issue(1, "status:ready", body="touches: src/a.py")]
        cap = tb.capacity(ready, held)
        self.assertEqual(cap["concurrent"], [])

    def test_issue_without_touches_cannot_be_counted(self):
        ready = [issue(1, "status:ready", body="no metadata")]
        cap = tb.capacity(ready, [])
        self.assertEqual(cap["concurrent"], [])
        self.assertIn("no touches", cap["deferred"][0][1])


if __name__ == "__main__":
    unittest.main()
