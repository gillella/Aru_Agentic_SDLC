"""Contract tests for the run-aru-factory entrypoint skill.

The skill is prose, so these assert the properties a reader depends on rather
than behaviour of code: that every mode exists and resolves somewhere real,
that the stop conditions cannot silently shrink, and - the one that matters
most over time - that the entrypoint keeps *delegating* instead of growing its
own copy of the procedures it points at. A router that forks its targets stops
being a router the first time one of them changes.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "run-aru-factory" / "SKILL.md"
ROUTER = ROOT / "skills" / "aru-agentic-sdlc" / "SKILL.md"
FLEET_PROMPT = ROOT / "prompts" / "fleet-worker.md"
BOARD_WORKFLOW = ROOT / "docs" / "project_board_workflow.md"
IMPLEMENT_SKILL = ROOT / "skills" / "implement-next-issue" / "SKILL.md"
DESKTOP_ADAPTERS = (
    ROOT / "templates" / "integrations" / "codex" / "instructions.md",
    ROOT / "templates" / "integrations" / "claude" / "CLAUDE.md",
    ROOT / "templates" / "integrations" / "antigravity" / "AGENTS.md",
    ROOT / "templates" / "cursor" / "commands" / "run-aru-factory.md",
    ROOT / "templates" / "cursor" / "commands" / "continue.md",
)
CURSOR_CODE_REVIEW = ROOT / "templates" / "cursor" / "commands" / "code-review.md"
CURSOR_USER_RULES = ROOT / "templates" / "cursor" / "user-rules-aru-agentic-sdlc.md"

MODES = ("adopt", "status", "next", "loop", "doctor")


def flat(text):
    """Collapses whitespace so assertions survive line wrapping.

    These files are hand-wrapped prose. Matching an exact newline position
    makes a test fail when someone rewraps a paragraph, which trains people
    to edit the test rather than read it.
    """
    return " ".join(text.split()).lower()


def skill_text():
    return SKILL.read_text(encoding="utf-8")


def frontmatter(text):
    """The YAML block between the leading --- fences, as raw lines."""
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    return match.group(1) if match else ""


class SkillExistsTests(unittest.TestCase):
    def test_the_skill_file_is_present(self):
        self.assertTrue(SKILL.is_file(), f"missing {SKILL}")

    def test_frontmatter_names_the_skill(self):
        fm = frontmatter(skill_text())
        self.assertIn("name: run-aru-factory", fm)

    def test_description_carries_natural_language_triggers(self):
        """Discovery is the whole point of an entrypoint.

        An agent finds this skill by description match, so the phrases a user
        would actually say have to appear in it.
        """
        description = frontmatter(skill_text()).lower()
        for phrase in (
            "please continue",
            "run the factory",
            "work the project board",
            "continue development",
            "adopt this project",
        ):
            self.assertIn(phrase, description, f"trigger missing: {phrase}")


class ModeTests(unittest.TestCase):
    def test_every_mode_is_documented(self):
        text = skill_text()
        for mode in MODES:
            self.assertRegex(
                text, rf"`{mode}`",
                f"mode not documented: {mode}",
            )

    def test_each_mode_resolves_to_something_real(self):
        """A mode that names no target is a promise the skill cannot keep."""
        text = skill_text()
        targets = {
            "adopt": "init-agent-project",
            "status": "fleet_status.py",
            "next": "fetch_next_work.py",
            "loop": "fleet-worker.md",
            "doctor": "doctor_local_agent_integrations.py",
        }
        for mode, target in targets.items():
            self.assertIn(target, text, f"{mode} names no target ({target})")

    def test_an_unknown_mode_is_rejected_rather_than_defaulted(self):
        """Guessing starts real work the user did not ask for.

        Silently treating an unrecognised mode as `next` would claim an issue
        and open a PR off a typo.
        """
        text = flat(skill_text())
        self.assertIn("unrecognised mode is an error", text)
        self.assertIn("never silently fall through", text)

    def test_doctor_names_the_install_link_command(self):
        """#34 landed: doctor mode must name the real command and exit contract."""
        text = skill_text()
        self.assertIn("doctor_local_agent_integrations.py", text)
        self.assertIn("Exit `0` healthy", text)
        self.assertNotIn("does not exist yet", text)
        self.assertNotIn("not yet diagnosable", text)


class ContinuityContractTests(unittest.TestCase):
    """Recoverable states must not quietly become desktop-task exits."""

    def test_only_operator_stop_or_human_intervention_ends_loop(self):
        text = flat(skill_text())
        self.assertIn("only when the operator explicitly stops", text)
        self.assertIn("human intervention", text)

    def test_recoverable_states_do_not_end_the_desktop_loop(self):
        text = flat(skill_text())
        for condition in ("idle", "complete", "review/ci/dependency", "exits `1`"):
            self.assertIn(condition, text, f"recoverable condition missing: {condition}")
        self.assertIn("do not emit a final response", text)

    def test_low_context_is_recovered_not_a_stop_condition(self):
        text = flat(skill_text())
        self.assertIn("context is running short", text)
        self.assertIn("context compaction", text)

    def test_desktop_task_is_not_replaced_by_a_cli_agent(self):
        text = flat(skill_text())
        self.assertIn("task the operator started owns the loop", text)
        self.assertIn("optional headless cli mode", text)
        self.assertIn("never use ui scripting", text)


class IdentityTests(unittest.TestCase):
    def test_identity_examples_match_helper_contracts(self):
        text = flat(skill_text())
        self.assertIn("picker derives a stable id", text)
        self.assertIn("model family is optional", text)
        self.assertIn("`create_pr.py` requires", text)
        self.assertIn("--agent", text)
        self.assertNotIn("every claim and pr needs", text)

    def test_fleet_prompt_names_top_level_agent_handoff(self):
        text = flat(FLEET_PROMPT.read_text(encoding="utf-8"))
        self.assertIn("picker json top-level `agent` field is the resolved identity", text)
        self.assertIn("use that exact value as `<agent_id>`", text)
        self.assertIn("claim_issue.py", text)
        self.assertIn("create_pr.py", text)

    def test_the_reason_identity_matters_is_stated(self):
        """Without the why, the flags read as ceremony and get dropped."""
        text = flat(skill_text())
        self.assertIn("same github user", text)

    def test_fleet_prompt_reuses_the_picker_resolved_identity(self):
        text = FLEET_PROMPT.read_text(encoding="utf-8")
        self.assertIn("top-level `agent` field", text)
        self.assertIn("resolved identity for this session", text)
        self.assertIn("later `claim_issue.py` and `create_pr.py` calls", text)


class GovernanceTests(unittest.TestCase):
    def test_core_guarantees_are_restated(self):
        text = flat(skill_text())
        for rule in ("issue-first", "worktree", "touches:", "closes #"):
            self.assertIn(rule, text, f"guarantee missing: {rule}")

    def test_coding_agent_review_is_refused(self):
        for text in (
            flat(skill_text()),
            flat(FLEET_PROMPT.read_text(encoding="utf-8")),
        ):
            self.assertIn("coding agents never review", text)

    def test_coderabbit_is_the_only_review_path(self):
        text = skill_text()
        self.assertIn("CodeRabbit", text)
        self.assertIn("rejects coding-agent", text)
        self.assertNotIn("gh pr review --approve", text)

    def test_merging_goes_through_the_gate_only(self):
        text = skill_text()
        self.assertIn("merge_pr.py", text)
        self.assertIn("gh pr merge", text)  # named as forbidden
        self.assertNotIn("gh pr merge --", text)
        self.assertIn("including the implementation author", text)
        self.assertNotIn("never a PR you authored", text)

    def test_retired_review_skill_is_self_contained(self):
        review = (ROOT / "skills" / "code-review" / "SKILL.md").read_text()
        self.assertNotIn("SKILL.md", review)
        self.assertNotIn("skills/address-pr-feedback", review)

    def test_cursor_code_review_command_is_a_refusal_router(self):
        text = CURSOR_CODE_REVIEW.read_text(encoding="utf-8")
        self.assertEqual(
            text.strip(),
            "\n".join([
                "Refuse coding-agent pull-request review under Aru_Agentic_SDLC.",
                "",
                "The assigned review-pool service alone reviews pull requests in this repository; coding agents never review.",
                "Route assigned-service review findings back to the factory picker to remediate them instead.",
                "Do not inspect the PR, run `gh pr review`, open a review workspace, or submit review comments.",
            ]),
        )
        lowered = text.lower()
        self.assertNotIn("approve", lowered)
        self.assertNotIn("request changes", lowered)
        self.assertNotIn("inspect the pr and", lowered)
        self.assertNotIn("review worktree", lowered)

    def test_cursor_user_rules_route_review_to_remediation(self):
        text = CURSOR_USER_RULES.read_text(encoding="utf-8").lower()
        self.assertIn("agents remediate findings", text)
        self.assertNotIn("auto-assign a free identity", text)
        self.assertIn("helper-specific contracts", text)

    def test_legacy_review_state_releases_claim_before_looping(self):
        for text in (
            flat(skill_text()),
            flat(FLEET_PROMPT.read_text(encoding="utf-8")),
        ):
            self.assertIn("work.type=review", text)
            self.assertIn("claim_issue.py", text)
            self.assertIn("--release", text)
            self.assertIn("return to the picker", text)

    def test_error_work_state_reports_reason_and_retries(self):
        text = flat(FLEET_PROMPT.read_text(encoding="utf-8"))
        self.assertIn("work.type", text)
        self.assertIn("`error`", text)
        self.assertIn("report `work.reason`", text)
        self.assertIn("start no work", text)
        self.assertIn("wait/retry path", text)

    def test_picker_claim_semantics_are_work_type_specific(self):
        for text in (
            flat(skill_text()),
            flat(FLEET_PROMPT.read_text(encoding="utf-8")),
        ):
            self.assertIn("feedback", text)
            self.assertIn("error", text)
            self.assertIn("idle", text)
            self.assertIn("returned without claims", text)
            self.assertIn("merge", text)
            self.assertIn("non-resume issue", text)
            self.assertIn("claim mutations", text)

    def test_merge_commands_preserve_picker_expected_head(self):
        for text in (
            flat(skill_text()),
            flat(BOARD_WORKFLOW.read_text(encoding="utf-8")),
            flat(FLEET_PROMPT.read_text(encoding="utf-8")),
        ):
            self.assertIn("--expected-head <head_sha>", text)
            self.assertIn("head_sha", text)

    def test_worktree_cleanup_uses_governed_helpers(self):
        board = flat(BOARD_WORKFLOW.read_text(encoding="utf-8"))
        implement = flat(IMPLEMENT_SKILL.read_text(encoding="utf-8"))
        self.assertIn("cleanup_worktrees.py", board)
        self.assertIn("--repo <repo_root>", board)
        self.assertIn("merge_pr.py", board)
        self.assertNotIn("remove temporary worktree directories with `git worktree remove .worktrees/<dir>`", board)
        self.assertIn("keep the active pr worktree", implement)
        self.assertIn("merge or remediation completion", implement)
        self.assertNotIn("clean up worktree directory if needed", implement)
        self.assertIn("later commit on the pr branch or by a reply that begins `withdrawn:`", implement)
        self.assertIn("thread resolution alone is never sufficient", implement)


class DelegationTests(unittest.TestCase):
    """The entrypoint must route, not fork.

    Every procedure it points at is maintained elsewhere. A copy here would
    drift the first time one of them changes, and the copy is what an agent
    would follow.
    """

    def test_it_references_the_skills_it_routes_to(self):
        text = skill_text()
        for skill in (
            "init-agent-project",
            "implement-next-issue",
            "address-pr-feedback",
        ):
            self.assertIn(skill, text, f"unreferenced route: {skill}")

    def test_research_picker_skill_survives_both_canonical_consumers(self):
        entrypoint = flat(skill_text())
        fleet = flat(FLEET_PROMPT.read_text(encoding="utf-8"))
        for consumer in (entrypoint, fleet):
            self.assertIn("skill: research", consumer)
            self.assertIn("skills/research/skill.md", consumer)
            self.assertIn("work.skill", consumer)
        self.assertNotIn(
            "| `issue` | `implement-next-issue` |", skill_text()
        )
        prompt = FLEET_PROMPT.read_text(encoding="utf-8")
        research_branch = prompt.split("##### Research issue", 1)[1].split(
            "##### Implementation issue", 1
        )[0]
        self.assertIn("return to\nthe top of the loop", research_branch)
        self.assertIn("Do not execute the implementation sequence", research_branch)
        for forbidden in ("create_branch.py", "git push", "create_pr.py"):
            self.assertNotIn(forbidden, research_branch)

    def test_it_does_not_restate_the_implementation_procedure(self):
        """Concrete markers of a forked procedure rather than a pointer."""
        text = skill_text()
        for forked in ("git rebase origin/main", "git push -u origin", "--worktree"):
            self.assertNotIn(
                forked, text,
                f"entrypoint restates a delegated step: {forked!r}",
            )

    def test_it_stays_short_enough_to_stay_a_router(self):
        """A soft ceiling, deliberately generous.

        The number is arbitrary; crossing it is the signal to check whether a
        procedure has been copied in rather than pointed at.
        """
        lines = len(skill_text().splitlines())
        self.assertLess(lines, 240, f"entrypoint has grown to {lines} lines")


class WiringTests(unittest.TestCase):
    def test_the_router_offers_the_entrypoint(self):
        self.assertIn("run-aru-factory", ROUTER.read_text(encoding="utf-8"))

    def test_please_continue_is_loop_not_implement_next_issue(self):
        """Bare continue used to skip review and merge.

        The picker order is feedback → merge → review → issue. Routing
        'continue' to implement-next-issue drops the first three.
        """
        router = ROUTER.read_text(encoding="utf-8")
        skill = flat(skill_text())
        self.assertIn("please continue", frontmatter(skill_text()).lower())
        self.assertIn("loop", skill)
        continue_lines = [line for line in router.splitlines() if "continue" in line.lower() and "|" in line]
        self.assertTrue(continue_lines, "router has no continue row")
        joined = " ".join(continue_lines).lower()
        self.assertIn("run-aru-factory", joined)
        self.assertNotIn("implement-next-issue", joined)
        cursor = (ROOT / "docs" / "cursor-integration.md").read_text(encoding="utf-8")
        self.assertIn("pick feedback → merge → issue", cursor)
        self.assertNotIn("pick feedback → merge → review → issue", cursor)

    def test_loop_pacing_is_dynamic(self):
        text = flat(skill_text())
        self.assertIn("pace dynamically", text)
        self.assertIn("fixed interval", text)

    def test_the_fleet_prompt_points_at_the_entrypoint(self):
        text = FLEET_PROMPT.read_text(encoding="utf-8")
        self.assertIn("run-aru-factory", text)

    def test_the_fleet_prompt_remains_the_loop_authority(self):
        """Two descriptions of one loop is the failure to avoid.

        The prompt must keep saying it owns the contract, so the skill cannot
        quietly become a second source of truth.
        """
        text = flat(FLEET_PROMPT.read_text(encoding="utf-8"))
        self.assertIn("authority on what the loop does", text)

    def test_every_desktop_adapter_preserves_the_current_project_task(self):
        for adapter in DESKTOP_ADAPTERS:
            text = flat(adapter.read_text(encoding="utf-8"))
            self.assertIn("desktop app", text, str(adapter))
            self.assertIn("do not replace", text, str(adapter))


class CursorRuleTests(unittest.TestCase):
    def test_cursor_rule_uses_remediation_wording(self):
        rule = (
            ROOT / "templates" / "cursor" / "rules" / "aru-agentic-sdlc.mdc"
        ).read_text(encoding="utf-8")
        self.assertIn("Feature and remediation work", rule)
        self.assertNotIn("Feature and review work", rule)


if __name__ == "__main__":
    unittest.main()
