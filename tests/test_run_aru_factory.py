# line-ceiling: 650
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
REVIEW_SKILL = ROOT / "skills" / "code-review" / "SKILL.md"
CURSOR_CODE_REVIEW = ROOT / "templates" / "cursor" / "commands" / "code-review.md"
CURSOR_USER_RULES = ROOT / "templates" / "cursor" / "user-rules-aru-agentic-sdlc.md"

MODES = ("adopt", "status", "next", "loop", "doctor")

# Review machinery #412 removed and #435 did not restore. Listing exact phrases
# only catches the wording that happened to be retired: "reviewer rotation"
# would be rejected while "the picker rotates reviewers" walked straight in. So
# these match the *concepts*, and a match is judged by its context below.
RETIRED_REVIEW_MACHINERY = (
    r"rotat(?:e|es|ed|ing|ion)|round[- ]?robin",
    r"fail[- ]?over|hand(?:s|ed|ing)? off to (?:another|the next) review",
    r"capacity|load[- ]balanc(?:e|es|ed|ing)|ledger",
    r"pool|roster|queue",
    r"dashboard|schedul(?:e|es|ed|ing|er)",
)

# A skill is allowed to *name* retired machinery, and both of these do -- that
# is what a denial is. What it may not do is describe the machinery as
# something the factory has, so every match must sit inside a denial.
DENIAL_MARKER = re.compile(r"\b(?:never|not|no|nor|neither|without|forbidden|refuses?)\b")

# A denial only speaks for the clause it stands in. A sentence-wide lookback let
# a denial of one thing vouch for a different mechanism raised later in the same
# sentence, so "not a review queue; the picker rotates reviewers" read as denied
# (#454). The lookback therefore stops at the nearest break before the match:
# the end of the previous sentence, or a boundary that starts a new predicate.
SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+|\n")

# A fresh subject after a connector is what separates "; the picker rotates"
# from the coordinated denials these skills carry ("not a review queue,
# rotation, scheduler"), whose later items continue the denied list rather than
# assert anything. Indefinite subjects are left out on purpose: "or a roster"
# is far more often the tail of a denial than the head of a restoration.
SUBJECT = r"(?:the|this|that|these|those|it|its|they|their|we|our|you|each|every|another)"
CLAUSE_BREAK = re.compile(
    rf";|:\s|\s--\s|\u2014"
    rf"|\b(?:and|but|or|yet|so|while|then|though|whereas)\s+{SUBJECT}\b"
    rf"|,\s*{SUBJECT}\b"
)


def clause_start(text, position):
    """Offset where the clause containing `position` begins."""
    ends = [match.end() for match in SENTENCE_BREAK.finditer(text, 0, position)]
    ends += [match.end() for match in CLAUSE_BREAK.finditer(text, 0, position)]
    return max(ends, default=0)


def clause_end(text, position):
    """Offset where the clause containing `position` ends."""
    breaks = (SENTENCE_BREAK.search(text, position), CLAUSE_BREAK.search(text, position))
    return min([match.start() for match in breaks if match], default=len(text))


def sentence_with(text, phrase):
    """The sentence holding `phrase`, so a clause is read in its own context."""
    position = text.index(phrase)
    starts = [match.end() for match in SENTENCE_BREAK.finditer(text, 0, position)]
    following = SENTENCE_BREAK.search(text, position)
    return text[max(starts, default=0):following.start() if following else len(text)]


def restored_machinery(text):
    """Machinery mentions in `text` that no denial in their clause rules out."""
    return [
        match.group(0)
        for pattern in RETIRED_REVIEW_MACHINERY
        for match in re.finditer(pattern, text)
        if not DENIAL_MARKER.search(text, clause_start(text, match.start()), match.start())
    ]


# The review contract is a pair of claims -- CodeRabbit by default, reassignment
# only by an operator -- and a document can carry every required phrase while a
# neighbouring clause hands the PR to the next provider on its own (#454). So
# automatic hand-off language is read the way machinery is: it may stand only
# where a denial in the same clause is what makes it true.
AUTOMATIC_CUE = re.compile(r"\b(?:automatic\w*|by default|on its own|unattended|itself)\b")
HANDOFF_CUE = re.compile(
    r"\b(?:reassign\w*|hand(?:s|ed|ing)?[\s-]off|falls?[\s-]back|moves?|switch\w*)\b"
)


def unqualified_automatic_handoff(text):
    """Clauses making provider hand-off automatic without denying it."""
    contradictions = []
    for cue in AUTOMATIC_CUE.finditer(text):
        clause = text[clause_start(text, cue.start()):clause_end(text, cue.end())]
        if HANDOFF_CUE.search(clause) and not DENIAL_MARKER.search(clause):
            contradictions.append(clause.strip())
    return contradictions


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

    def test_coding_agent_review_is_emergency_only(self):
        for text in (
            flat(skill_text()),
            flat(FLEET_PROMPT.read_text(encoding="utf-8")),
        ):
            self.assertIn("review:agent", text)
            self.assertIn("external exhaustion", text)
            self.assertNotIn("auto-assign a free identity", text)

    def test_review_paths_keep_external_default_and_agent_exception_narrow(self):
        """The router must name every authority without creating a scheduler."""
        text = skill_text()
        for provider in ("CodeRabbit", "Sourcery", "CodeAnt", "review:agent"):
            self.assertIn(provider, text, f"review path missing: {provider}")
        self.assertIn("emergency-only", text)
        self.assertIn("review:coderabbit", text)
        self.assertIn("reassignment is never automatic", text)
        self.assertIn("Only an operator", text)
        # Both operator-declared conditions, or the router forbids a fallback
        # the helper and the code-review skill both allow (#454).
        self.assertIn(
            "external exhaustion or an operator-declared excessive wait",
            flat(text),
        )
        self.assertIn("never creates a coding-agent review", text)
        self.assertIn("address-pr-feedback", text)
        self.assertEqual(
            [], unqualified_automatic_handoff(flat(text)), "router hands off automatically"
        )
        self.assertNotIn("gh pr review --approve", text)

    def test_merging_goes_through_the_gate_only(self):
        text = skill_text()
        self.assertIn("merge_pr.py", text)
        self.assertIn("gh pr merge", text)  # named as forbidden
        self.assertNotIn("gh pr merge --", text)
        self.assertIn("including the implementation author", text)
        self.assertNotIn("never a PR you authored", text)

    def test_retired_review_skill_is_self_contained(self):
        review = REVIEW_SKILL.read_text()
        self.assertNotIn("SKILL.md", review)
        self.assertNotIn("skills/address-pr-feedback", review)

    def test_review_skill_states_the_default_and_the_explicit_fallback(self):
        """CodeRabbit at creation; Sourcery/CodeAnt only on an operator move."""
        review = flat(REVIEW_SKILL.read_text(encoding="utf-8"))
        for clause in (
            "review:coderabbit",
            "only an operator may reassign",
            "sourcery or codeant",
            "reassignment is never automatic",
            "reassign_review.py",
            "external exhaustion or an operator-declared excessive wait",
            "not a review queue, rotation, scheduler",
        ):
            self.assertIn(clause, review, f"review contract missing: {clause}")
        # Presence anywhere is also satisfied by a document that says elsewhere
        # that the router hands off by itself (#454), so the two load-bearing
        # clauses are read in their sentence and contradictions are rejected.
        reassignment = sentence_with(review, "may reassign")
        self.assertIn("only an operator", reassignment)
        self.assertIn("sourcery or codeant", reassignment)
        emergency = sentence_with(review, "may review only when")
        for clause in (
            "reassign_review.py",
            "external exhaustion or an operator-declared excessive wait",
            "reviewer:<current_agent_id>",
        ):
            self.assertIn(clause, emergency, f"emergency assignment unbounded: {clause}")
        self.assertEqual(
            [], unqualified_automatic_handoff(review), "review skill hands off automatically"
        )

    def test_no_retired_review_machinery_is_restored(self):
        """AC3: no rotation, failover, capacity tracking, pool, or scheduler."""
        for path in (SKILL, REVIEW_SKILL):
            text = flat(path.read_text(encoding="utf-8"))
            self.assertEqual(
                [], restored_machinery(text), f"{path.name} restored review machinery"
            )

    def test_the_machinery_detector_survives_rewording(self):
        """The guard above is only worth its line count if it still bites.

        An absence assertion passes when the thing it looks for was merely
        renamed, so these are the rewordings #454 flagged: each must be caught,
        and the denials the two skills actually carry must not be.
        """
        for restored in (
            "the picker rotates review:agent across the fleet",
            "reviewer capacity tracking picks the least loaded agent",
            "automatic failover moves the pr to the next reviewer",
            "reviewers are drawn from the reviewer pool in round-robin order",
            "a scheduler dashboard shows each reviewer's backlog",
        ):
            self.assertTrue(restored_machinery(restored), f"undetected: {restored}")
        for denial in (
            "not a review queue, rotation, scheduler, or permission to select",
            "the picker resumes it but never creates a coding-agent review queue",
            "it does not track capacity, rotate agents, or create a second queue",
        ):
            self.assertEqual([], restored_machinery(denial), f"false positive: {denial}")

    def test_a_denial_does_not_reach_past_its_own_clause(self):
        """#454: a denial covered later prose in the same sentence.

        Both halves of each line below are things the skills genuinely say, and
        the denial is real -- but it answers reassignment, not the clause after
        it. A detector that lets the "never" carry across a full stop, a
        semicolon, or a conjunction goes quiet on the one shape it exists to
        catch: machinery reintroduced beside a denial of something else. The
        denials underneath coordinate one list, so they must stay clear.
        """
        for restored in (
            "reassignment is never automatic. the picker rotates reviewers",
            "this is not a review queue; the picker rotates reviewers",
            "reassignment is never automatic and the picker rotates reviewers",
            "reassignment is never automatic, the picker rotates reviewers",
            "not a review queue, rotation, or scheduler, but it rotates reviewers",
        ):
            self.assertEqual(["rotate"], restored_machinery(restored), f"undetected: {restored}")
        for denied in (
            "the picker never rotates reviewers",
            "not a review queue, rotation, scheduler, or permission to select",
            "it does not track capacity, rotate agents, or create a second queue",
        ):
            self.assertEqual([], restored_machinery(denied), f"false positive: {denied}")

    def test_the_hand_off_detector_reads_contradictions_in_context(self):
        """#454: required wording plus contradictory prose must still fail.

        Each contradiction keeps the contract's own sentence intact and adds a
        clause that undoes it -- the shape a presence-only assertion cannot
        see. The denials beneath it are what the two skills actually say.
        """
        for contradiction in (
            "reassignment is never automatic. the picker automatically reassigns the pr",
            "reassignment is never automatic, but the router falls back to codeant on its own",
            "when external review stalls the pr moves to codeant automatically",
        ):
            self.assertTrue(
                unqualified_automatic_handoff(contradiction), f"undetected: {contradiction}"
            )
        for denial in (
            "reassignment is never automatic",
            "only an operator may reassign it to sourcery or codeant",
            "the router never falls back to another provider automatically",
        ):
            self.assertEqual(
                [], unqualified_automatic_handoff(denial), f"false positive: {denial}"
            )

    def test_cursor_code_review_command_is_an_emergency_router(self):
        text = CURSOR_CODE_REVIEW.read_text(encoding="utf-8")
        self.assertIn("explicit", text)
        self.assertIn("review:agent", text)
        self.assertIn("external reviewer exhaustion", text)
        self.assertIn("different from the", text)
        self.assertIn("exact current head", text)
        lowered = text.lower()
        self.assertNotIn("auto-assign", lowered)
        self.assertNotIn("review any", lowered)

    def test_cursor_user_rules_route_review_to_remediation(self):
        text = CURSOR_USER_RULES.read_text(encoding="utf-8").lower()
        self.assertIn("remediate external findings", text)
        self.assertIn("review:agent", text)
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

    def test_loop_distinguishes_transient_picker_error_from_diagnostics(self):
        """A returned `error` work item is loop state, not a reason to diagnose.

        `fetch_next_work.py` emits `{"type": "error", ...}` as an ordinary,
        usable result, so a router that only says "diagnose a concrete
        picker/helper error" reads as licence to spend the tick's remaining
        budget on diagnostics.
        """
        loop = flat(skill_text().split("### loop", 1)[1].split("### doctor", 1)[0])
        self.assertIn("a returned `error` work item", loop)
        self.assertIn("transient loop state, not a concrete failure", loop)
        self.assertIn("yields no usable snapshot", loop)
        self.assertIn("full diagnostics are a separately declared attempt", loop)
        self.assertIn("zero follow-up github reads", loop)

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
        fleet = FLEET_PROMPT.read_text(encoding="utf-8")
        for text in (flat(skill_text()), flat(BOARD_WORKFLOW.read_text()), flat(fleet)):
            self.assertIn("--expected-head <head_sha>", text)
            self.assertIn("head_sha", text)
        merge = fleet.split("#### B.", 1)[1].split("#### C.", 1)[0]
        commands = re.findall(r"(?m)^\s*`(python3 [^`\n]+)`\s*$", merge)
        self.assertEqual(commands, ['python3 "$ARU_SDLC_HOME/scripts/merge_pr.py" --pr <N> --expected-head <HEAD_SHA>', 'python3 "$ARU_SDLC_HOME/scripts/claim_issue.py" --pr <N> --agent <AGENT_ID> --merge --release'])
        # Both merge contracts, not just the fleet prompt: the router's own
        # `### Merging` section is what a desktop agent reads, so a dry-run or a
        # second merge command reintroduced there has to fail too.
        contracts = (
            skill_text().split("### Merging", 1)[1].split("## References", 1)[0],
            merge,
        )
        for contract in contracts:
            helpers = re.findall(r"python3 [^`\n]*merge_pr\.py[^`\n]*", contract)
            with self.subTest(contract=contract[:40]):
                self.assertEqual(len(helpers), 1, helpers)
                self.assertIn("--expected-head <HEAD_SHA>", helpers[0])
                self.assertNotIn("--dry-run", helpers[0])

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

    def test_the_fleet_prompt_requires_focused_story_evidence(self):
        text = flat(FLEET_PROMPT.read_text(encoding="utf-8"))
        self.assertIn("every `verify:` predicate", text)
        self.assertIn("directly affected tests", text)
        self.assertIn("behavioral evidence", text)
        self.assertIn("phase and pre-release checkpoints", text)

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
