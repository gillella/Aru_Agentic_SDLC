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
DESKTOP_ADAPTERS = (
    ROOT / "templates" / "integrations" / "codex" / "instructions.md",
    ROOT / "templates" / "integrations" / "claude" / "CLAUDE.md",
    ROOT / "templates" / "integrations" / "antigravity" / "AGENTS.md",
    ROOT / "templates" / "cursor" / "commands" / "run-aru-factory.md",
    ROOT / "templates" / "cursor" / "commands" / "continue.md",
)

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

    def test_doctor_admits_what_it_cannot_yet_check(self):
        """#34 has not landed, so the mode must not imply full diagnosis.

        Claiming a clean setup it never verified is worse than reporting a gap.
        """
        text = skill_text()
        self.assertIn("does not exist yet", text)
        self.assertIn("#34", text)
        self.assertIn("not yet diagnosable", text)


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
    def test_agent_id_and_family_are_required(self):
        text = skill_text()
        self.assertIn("--agent", text)
        self.assertIn("--family", text)

    def test_the_reason_identity_matters_is_stated(self):
        """Without the why, the flags read as ceremony and get dropped."""
        text = flat(skill_text())
        self.assertIn("same github user", text)


class GovernanceTests(unittest.TestCase):
    def test_core_guarantees_are_restated(self):
        text = flat(skill_text())
        for rule in ("issue-first", "worktree", "touches:", "closes #"):
            self.assertIn(rule, text, f"guarantee missing: {rule}")

    def test_self_review_is_refused(self):
        text = flat(skill_text())
        self.assertIn("never review your own pr", text)

    def test_the_same_account_review_path_is_the_documented_one(self):
        """`--approve` is rejected by GitHub for the fleet's shared account.

        Regression guard: the skill must not drift back to advising it.
        """
        text = skill_text()
        self.assertIn("--comment", text)
        self.assertIn("reviewed-by", text)
        self.assertNotIn("gh pr review --approve", text)

    def test_merging_goes_through_the_gate_only(self):
        text = skill_text()
        self.assertIn("merge_pr.py", text)
        self.assertIn("gh pr merge", text)  # named as forbidden
        self.assertNotIn("gh pr merge --", text)


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
            "code-review",
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
        # The continue row must name run-aru-factory, not implement-next-issue.
        continue_lines = [
            line for line in router.splitlines()
            if "continue" in line.lower() and "|" in line
        ]
        self.assertTrue(continue_lines, "router has no continue row")
        joined = " ".join(continue_lines).lower()
        self.assertIn("run-aru-factory", joined)
        self.assertNotIn("implement-next-issue", joined)

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


if __name__ == "__main__":
    unittest.main()
