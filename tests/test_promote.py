import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import promote  # noqa: E402


SHA = "a" * 40
CHECKPOINT = "ckpt/162-aaaaaaa"
REPO = "gillella/Aru_Agentic_SDLC"
TAG_OBJECT = "c" * 40


def run_data(
    run_id=41,
    workflow="Deploy Preview",
    title=f"Deploy Preview for {SHA}",
    conclusion="success",
    event="workflow_dispatch",
):
    return {
        "databaseId": run_id,
        "conclusion": conclusion,
        "displayTitle": title,
        "event": event,
        "workflowName": workflow,
        "url": f"https://github.com/{REPO}/actions/runs/{run_id}",
        "headSha": "b" * 40,
    }


class TransitionTests(unittest.TestCase):
    def test_only_directional_adjacent_transitions_are_valid(self):
        self.assertTrue(promote.valid_transition("preview", "staging", "forward"))
        self.assertTrue(promote.valid_transition("staging", "production", "forward"))
        self.assertTrue(promote.valid_transition("production", "staging", "reverse"))
        self.assertTrue(promote.valid_transition("staging", "preview", "reverse"))
        self.assertFalse(promote.valid_transition("preview", "production", "forward"))
        self.assertFalse(promote.valid_transition("production", "staging", "forward"))
        self.assertFalse(promote.valid_transition("preview", "staging", "reverse"))
        self.assertFalse(promote.valid_transition("staging", "staging", "reverse"))
        self.assertFalse(promote.valid_transition("staging", "production", "sideways"))


class EvidenceTests(unittest.TestCase):
    @patch.object(promote, "_read_run")
    def test_preview_evidence_binds_successful_exact_run(self, read_run):
        read_run.return_value = run_data()
        ok, value = promote.verify_prior_stage_evidence(41, "preview", SHA, REPO)
        self.assertTrue(ok)
        self.assertEqual(value, f"https://github.com/{REPO}/actions/runs/41")

    @patch.object(promote, "_read_run")
    def test_staging_evidence_requires_prior_staging_promotion(self, read_run):
        read_run.return_value = run_data(
            workflow=promote.WORKFLOW_DISPLAY_NAME,
            title=f"Audit-only promotion staging for {SHA} from preview [forward] (token)",
            event="repository_dispatch",
        )
        self.assertTrue(
            promote.verify_prior_stage_evidence(41, "staging", SHA, REPO)[0]
        )
        read_run.return_value = run_data(
            workflow=promote.WORKFLOW_DISPLAY_NAME,
            title=f"Audit-only promotion production for {SHA} from staging [forward] (token)",
            event="repository_dispatch",
        )
        self.assertFalse(
            promote.verify_prior_stage_evidence(41, "staging", SHA, REPO)[0]
        )

    @patch.object(promote, "_read_run")
    def test_evidence_rejects_failure_or_foreign_url(self, read_run):
        read_run.return_value = run_data(conclusion="failure")
        self.assertFalse(
            promote.verify_prior_stage_evidence(41, "preview", SHA, REPO)[0]
        )
        foreign = run_data()
        foreign["url"] = "https://github.com/other/repo/actions/runs/41"
        read_run.return_value = foreign
        self.assertFalse(
            promote.verify_prior_stage_evidence(41, "preview", SHA, REPO)[0]
        )

    @patch.object(promote, "_read_run")
    def test_reverse_reference_binds_exact_commit_and_inverse_transition(self, read_run):
        read_run.return_value = run_data(
            workflow=promote.WORKFLOW_DISPLAY_NAME,
            title=(
                f"Audit-only promotion production for {SHA} from staging "
                f"[forward] ({'f' * 32})"
            ),
            event="repository_dispatch",
        )
        self.assertTrue(
            promote.verify_reverse_reference(
                41, "production", "staging", SHA, REPO
            )[0]
        )
        self.assertFalse(
            promote.verify_reverse_reference(
                41, "production", "staging", "b" * 40, REPO
            )[0]
        )
        self.assertFalse(
            promote.verify_reverse_reference(
                41, "staging", "preview", SHA, REPO
            )[0]
        )

    @patch.object(promote, "_read_run")
    def test_promotion_evidence_rejects_preview_event_type(self, read_run):
        read_run.return_value = run_data(
            workflow=promote.WORKFLOW_DISPLAY_NAME,
            title=f"Audit-only promotion staging for {SHA} from preview [forward]",
            event="workflow_dispatch",
        )
        self.assertFalse(
            promote.verify_prior_stage_evidence(41, "staging", SHA, REPO)[0]
        )


class CheckpointTests(unittest.TestCase):
    @patch.object(promote, "run_cmd")
    def test_checkpoint_binds_commit_and_included_issues(self, run_cmd):
        message = (
            "checkpoint: PR #162\n\n"
            "issues:      #109, #110\n"
            f"merged as:   {SHA}\n"
        )
        run_cmd.side_effect = [
            (0, f"{TAG_OBJECT}\trefs/tags/{CHECKPOINT}\n{SHA}\trefs/tags/{CHECKPOINT}^{{}}", ""),
            (0, "commit\n", ""),
            (0, TAG_OBJECT, ""),
            (0, message, ""),
        ]
        ok, _ = promote.verify_checkpoint(CHECKPOINT, SHA, [109, 110])
        self.assertTrue(ok)

    @patch.object(promote, "run_cmd")
    def test_checkpoint_rejects_issue_not_in_annotation(self, run_cmd):
        message = f"issues:      #109\nmerged as:   {SHA}\n"
        run_cmd.side_effect = [
            (0, f"{TAG_OBJECT}\trefs/tags/{CHECKPOINT}\n{SHA}\trefs/tags/{CHECKPOINT}^{{}}", ""),
            (0, "commit\n", ""),
            (0, TAG_OBJECT, ""),
            (0, message, ""),
        ]
        ok, reason = promote.verify_checkpoint(CHECKPOINT, SHA, [110])
        self.assertFalse(ok)
        self.assertIn("included issues", reason)

    @patch.object(promote, "run_cmd")
    def test_checkpoint_rejects_partial_issue_set(self, run_cmd):
        message = f"issues:      #109, #110\nmerged as:   {SHA}\n"
        run_cmd.side_effect = [
            (0, f"{TAG_OBJECT}\trefs/tags/{CHECKPOINT}\n{SHA}\trefs/tags/{CHECKPOINT}^{{}}", ""),
            (0, "commit\n", ""),
            (0, TAG_OBJECT, ""),
            (0, message, ""),
        ]
        ok, reason = promote.verify_checkpoint(CHECKPOINT, SHA, [109])
        self.assertFalse(ok)
        self.assertIn("exactly match", reason)

    def test_checkpoint_name_must_match_commit(self):
        ok, reason = promote.verify_checkpoint("ckpt/162-bbbbbbb", SHA, [109])
        self.assertFalse(ok)
        self.assertIn("name", reason)


class IssueBindingTests(unittest.TestCase):
    @patch.object(promote, "run_cmd")
    def test_all_included_issues_must_be_repository_bound(self, run_cmd):
        run_cmd.side_effect = [
            (0, json.dumps({
                "number": 109,
                "state": "closed",
                "url": f"https://github.com/{REPO}/issues/109",
                "is_pr": False,
            }), ""),
            (0, json.dumps({
                "number": 110,
                "state": "open",
                "url": f"https://github.com/{REPO}/issues/110",
                "is_pr": False,
            }), ""),
        ]
        self.assertTrue(promote.verify_issues([109, 110], REPO)[0])

    @patch.object(promote, "run_cmd")
    def test_foreign_issue_fails_before_dispatch(self, run_cmd):
        run_cmd.return_value = (0, json.dumps({
            "number": 109,
            "state": "CLOSED",
            "url": "https://github.com/foreign/repo/issues/109",
            "is_pr": False,
        }), "")
        self.assertFalse(promote.verify_issues([109], REPO)[0])

    @patch.object(promote, "run_cmd")
    def test_pull_request_number_is_not_accepted_as_included_issue(self, run_cmd):
        run_cmd.return_value = (0, json.dumps({
            "number": 109,
            "state": "OPEN",
            "url": f"https://github.com/{REPO}/issues/109",
            "is_pr": True,
        }), "")
        self.assertFalse(promote.verify_issues([109], REPO)[0])


class DispatchTests(unittest.TestCase):
    @patch.object(promote, "verify_run_correlation", return_value=True)
    @patch.object(promote, "get_promotion_run_ids")
    @patch.object(promote, "run_cmd", return_value=(0, "", ""))
    def test_dispatch_passes_complete_governed_contract(
        self, run_cmd, existing_runs, verify_correlation
    ):
        existing_runs.side_effect = [{5}, {5, 77}]
        run_id = promote.dispatch_promotion(
            SHA,
            CHECKPOINT,
            [109, 110],
            "preview",
            "staging",
            41,
            "main",
            REPO,
        )
        self.assertEqual(run_id, 77)
        argv = run_cmd.call_args.args[0]
        self.assertEqual(
            argv[:5], ["gh", "api", "--method", "POST", f"repos/{REPO}/dispatches"]
        )
        joined = " ".join(argv)
        self.assertIn("event_type=aru-promotion", joined)
        self.assertIn(f"client_payload[commit_sha]={SHA}", joined)
        self.assertIn(f"client_payload[checkpoint]={CHECKPOINT}", joined)
        self.assertIn("client_payload[issues]=109,110", joined)
        self.assertIn("client_payload[from_environment]=preview", joined)
        self.assertIn("client_payload[to_environment]=staging", joined)
        self.assertIn("client_payload[evidence_run]=41", joined)
        self.assertIn("client_payload[direction]=forward", joined)
        verify_correlation.assert_called_once()

    def test_dispatch_rejects_environment_skip(self):
        self.assertIsNone(
            promote.dispatch_promotion(
                SHA, CHECKPOINT, [109], "preview", "production", 41, "main", REPO
            )
        )

    @patch.object(promote, "verify_run_correlation", return_value=True)
    @patch.object(promote, "get_promotion_run_ids")
    @patch.object(promote, "run_cmd", return_value=(0, "", ""))
    def test_dispatch_supports_true_inverse_transition(
        self, run_cmd, existing_runs, verify_correlation
    ):
        existing_runs.side_effect = [{5}, {5, 78}]
        run_id = promote.dispatch_promotion(
            SHA,
            CHECKPOINT,
            [109],
            "production",
            "staging",
            41,
            "main",
            REPO,
            reverse_of=77,
        )
        self.assertEqual(run_id, 78)
        joined = " ".join(run_cmd.call_args.args[0])
        self.assertIn("client_payload[direction]=reverse", joined)
        self.assertIn("client_payload[reverse_of_run]=77", joined)
        verify_correlation.assert_called_once()


class AuditTrailTests(unittest.TestCase):
    @patch.object(promote, "run_cmd")
    def test_deployment_record_binds_run_commit_checkpoint_and_issues(self, run_cmd):
        run_url = f"https://github.com/{REPO}/actions/runs/77"
        deployment = {
            "id": 987,
            "ref": SHA,
            "sha": SHA,
            "environment": "staging",
            "description": f"Aru audit-only forward promotion {CHECKPOINT}; run 77",
            "payload": {
                "audit_only": True,
                "checkpoint": CHECKPOINT,
                "included_issues": [109, 110],
                "direction": "forward",
                "workflow_run": 77,
                "scope": "github-state-only",
            },
        }
        status = {
            "state": "success",
            "environment": "staging",
            "log_url": run_url,
            "description": "Audit-only GitHub state; no runnable build movement claimed",
        }
        run_cmd.side_effect = [
            (0, json.dumps([deployment]), ""),
            (0, json.dumps([status]), ""),
        ]
        self.assertEqual(
            promote.verify_deployment_record(
                77,
                run_url,
                SHA,
                CHECKPOINT,
                [109, 110],
                "staging",
                "forward",
                REPO,
            ),
            (True, "987"),
        )

    @patch.object(promote, "run_cmd")
    def test_deployment_record_fails_closed_on_ambiguous_match(self, run_cmd):
        run_cmd.return_value = (0, "[]", "")
        ok, reason = promote.verify_deployment_record(
            77,
            f"https://github.com/{REPO}/actions/runs/77",
            SHA,
            CHECKPOINT,
            [109],
            "staging",
            "forward",
            REPO,
        )
        self.assertFalse(ok)
        self.assertIn("not unique", reason)

    @patch.object(promote, "run_cmd")
    def test_record_is_idempotent_and_complete(self, run_cmd):
        run_cmd.side_effect = [(0, "", ""), (0, "comment-url", "")]
        ok = promote.record_promotion(
            109,
            77,
            f"https://github.com/{REPO}/actions/runs/77",
            "preview",
            "staging",
            SHA,
            CHECKPOINT,
            [109, 110],
            41,
            REPO,
            "987",
        )
        self.assertTrue(ok)
        body = run_cmd.call_args_list[1].args[0][-1]
        self.assertIn(SHA, body)
        self.assertIn(CHECKPOINT, body)
        self.assertIn("#109, #110", body)
        self.assertIn("Prior-stage evidence: 41", body)
        self.assertIn("GitHub deployment record: `987`", body)
        self.assertIn(promote.AUDIT_ONLY_NOTICE, body)
        self.assertIn("no runnable build", body)

        marker = promote._promotion_marker(77, "staging", SHA)
        run_cmd.reset_mock()
        run_cmd.side_effect = None
        run_cmd.return_value = (0, marker, "")
        self.assertTrue(
            promote.record_promotion(
                109,
                77,
                f"https://github.com/{REPO}/actions/runs/77",
                "preview",
                "staging",
                SHA,
                CHECKPOINT,
                [109],
                41,
                REPO,
                "987",
            )
        )
        run_cmd.assert_called_once()


class PromotionLifecycleTests(unittest.TestCase):
    def patches(self, *, outcome=None):
        return (
            patch.object(promote, "get_default_branch", return_value="main"),
            patch.object(promote, "get_repo_slug", return_value=REPO),
            patch.object(promote, "verify_commit_merged", return_value=(True, SHA)),
            patch.object(promote, "verify_checkpoint", return_value=(True, "checkpoint")),
            patch.object(promote, "verify_issues", return_value=(True, "issues")),
            patch.object(
                promote,
                "verify_prior_stage_evidence",
                return_value=(True, f"https://github.com/{REPO}/actions/runs/41"),
            ),
            patch.object(promote, "dispatch_promotion", return_value=77),
            patch.object(
                promote,
                "wait_for_run",
                return_value=outcome
                or promote.RunOutcome(
                    True, "success", f"https://github.com/{REPO}/actions/runs/77"
                ),
            ),
            patch.object(
                promote, "verify_deployment_record", return_value=(True, "987")
            ),
            patch.object(promote, "record_promotion", return_value=True),
        )

    def test_records_preview_to_staging_to_production_audit_state(self):
        for source, target, evidence in (
            ("preview", "staging", 41),
            ("staging", "production", 77),
        ):
            contexts = self.patches()
            with contexts[0], contexts[1], contexts[2], contexts[3], contexts[4], \
                    contexts[5], contexts[6], contexts[7], contexts[8], \
                    contexts[9] as record:
                self.assertEqual(
                    promote.promote(
                        SHA, CHECKPOINT, [109, 110], source, target, evidence
                    ),
                    0,
                )
                self.assertEqual(record.call_count, 2)

    @patch.object(promote, "verify_reverse_reference", return_value=(True, "url"))
    def test_reverse_path_uses_inverse_transition_and_records_prior_promotion(
        self, reverse_reference
    ):
        contexts = self.patches()
        with contexts[0], contexts[1], contexts[2], contexts[3], contexts[4], \
                contexts[5] as evidence_check, contexts[6] as dispatch, \
                contexts[7], contexts[8], \
                contexts[9] as record:
            self.assertEqual(
                promote.promote(
                    SHA,
                    CHECKPOINT,
                    [109],
                    "production",
                    "staging",
                    41,
                    reverse_of=66,
                ),
                0,
            )
        reverse_reference.assert_called_once_with(
            66, "production", "staging", SHA, REPO
        )
        evidence_check.assert_called_once_with(41, "staging", SHA, REPO)
        self.assertEqual(dispatch.call_args.kwargs["reverse_of"], 66)
        self.assertEqual(record.call_args.kwargs["reverse_of"], 66)

    @patch.object(promote, "get_default_branch")
    def test_abbreviated_commit_is_rejected_before_repository_queries(self, default):
        self.assertEqual(
            promote.promote(
                "a" * 7, CHECKPOINT, [109], "preview", "staging", 41
            ),
            1,
        )
        default.assert_not_called()

    def test_dry_run_does_not_query_a_deployment_that_was_not_created(self):
        contexts = self.patches()
        with contexts[0], contexts[1], contexts[2], contexts[3], contexts[4], \
                contexts[5], contexts[6], contexts[7] as wait, \
                contexts[8] as deployment, contexts[9] as record:
            self.assertEqual(
                promote.promote(
                    SHA,
                    CHECKPOINT,
                    [109],
                    "preview",
                    "staging",
                    41,
                    dry_run=True,
                ),
                0,
            )
        wait.assert_not_called()
        deployment.assert_not_called()
        self.assertEqual(record.call_args.args[10], "dry-run")

    def test_failed_workflow_never_records_promotion(self):
        outcome = promote.RunOutcome(
            False, "failure", f"https://github.com/{REPO}/actions/runs/77"
        )
        contexts = self.patches(outcome=outcome)
        with contexts[0], contexts[1], contexts[2], contexts[3], contexts[4], \
                contexts[5], contexts[6], contexts[7], contexts[8], \
                contexts[9] as record:
            self.assertEqual(
                promote.promote(
                    SHA, CHECKPOINT, [109], "preview", "staging", 41
                ),
                1,
            )
            record.assert_not_called()


class RepositoryContractTests(unittest.TestCase):
    def test_skill_exposes_only_the_governed_command(self):
        content = (ROOT / "skills" / "deploy-preview" / "SKILL.md").read_text()
        self.assertIn('scripts/promote.py"', content)
        self.assertNotRegex(content, r"gh\s+(workflow|api)\s+(run|--method)")
        self.assertIn("[--issue <ANOTHER_INCLUDED_ISSUE> ...]", content)
        self.assertIn("[--dry-run]", content)
        self.assertIn("does not deploy, copy", content)
        self.assertIn("issue #234", content)

    def test_workflow_validates_before_selecting_environment(self):
        content = (ROOT / ".github" / "workflows" / "promote.yml").read_text()
        self.assertIn("name: Record Governed Promotion", content)
        self.assertIn("Audit-only promotion", content)
        self.assertIn("Validate governed promotion evidence", content)
        self.assertIn("deployments: write", content)
        workflow_permissions = content.split("permissions:", 1)[1].split(
            "concurrency:", 1
        )[0]
        self.assertNotIn("deployments: write", workflow_permissions)
        record_job = content.split("record-environment-state:", 1)[1]
        self.assertIn("deployments: write", record_job)
        self.assertNotIn("issues: write", content)
        self.assertIn("repository_dispatch", content)
        trigger_section = content.split("permissions:", 1)[0]
        self.assertNotIn("workflow_dispatch", trigger_section)
        self.assertIn("needs: validate", content)
        self.assertIn("environment: ${{ needs.validate.outputs.target_environment }}", content)
        self.assertNotIn("Record governed issue trail", content)
        self.assertIn("production:staging", content)
        self.assertIn("staging:preview", content)
        self.assertIn("log_url", content)
        self.assertNotIn("environment_url", content)
        self.assertIn("included_issues", content)
        self.assertIn('"${requested_issues}" == "${checkpoint_issues}"', content)
        self.assertIn('TARGET_SHA="${TARGET_SHA,,}"', content)
        self.assertIn('refs/tags/${CHECKPOINT}^{tag}', content)
        self.assertIn('^merged as:[[:space:]]+${TARGET_SHA}$', content)
        self.assertIn(
            "Audit-only promotion ${FROM_ENV} for ${TARGET_SHA} from ${TO_ENV} [forward]",
            content,
        )
        self.assertIn("github-state-only", content)
        self.assertIn("no runnable build movement claimed", content)
        self.assertIn("Mark incomplete audit-only deployment record failed", content)
        self.assertIn("failure() || cancelled()", content)
        self.assertIn("promotion run did not complete", content)

    def test_repository_contract_never_claims_hosted_promotion(self):
        workflow = (ROOT / ".github" / "workflows" / "promote.yml").read_text()
        skill = (ROOT / "skills" / "deploy-preview" / "SKILL.md").read_text()
        script = (ROOT / "scripts" / "promote.py").read_text()
        combined = "\n".join((workflow, skill, script)).lower()
        self.assertIn("audit-only", combined)
        self.assertIn("no runnable build", combined)
        self.assertNotIn("build actually moves", combined)

    @patch.object(promote, "promote", return_value=0)
    def test_cli_requires_complete_issue_first_evidence(self, promote_call):
        code = promote.main([
            "--commit", SHA,
            "--checkpoint", CHECKPOINT,
            "--issue", "109",
            "--from-environment", "preview",
            "--to-environment", "staging",
            "--evidence-run", "41",
        ])
        self.assertEqual(code, 0)
        promote_call.assert_called_once_with(
            SHA,
            CHECKPOINT,
            [109],
            "preview",
            "staging",
            41,
            reverse_of=None,
            workflow_name="promote.yml",
            dry_run=False,
        )


if __name__ == "__main__":
    unittest.main()
