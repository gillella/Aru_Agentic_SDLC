import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from increment_release import (  # noqa: E402
    IncrementReleaseError,
    create_checkpoint_tag,
    publish_increment_release,
)
import increment_release  # noqa: E402


RUN_URL = "https://github.com/gillella/Aru_Agentic_SDLC/actions/runs/12345"


def make_sample_increment(
    increment_id: str = "inc_0123456789abcdef0123",
    project_id: str = "proj_sample",
    lifecycle_state: str = "accepted",
    release_state: str = "unreleased",
    issue_scope: list = None,
    with_acceptance_evidence: bool = True,
    corrupt_evidence_url: bool = False,
    authenticated: bool = True,
) -> dict:
    if issue_scope is None:
        issue_scope = [101, 102]

    decisions = [
        {
            "decision": {
                "decision_id": f"{project_id}|T1234567|C1234567|evt_auth",
                "action": "authorize",
                "increment_id": increment_id,
                "project_id": project_id,
                "operator_user_id": "U1234567",
                "github_repository": "gillella/Aru_Agentic_SDLC",
                "kind": "normal",
                "control_issue": 50,
                "issue_scope": issue_scope,
                "baseline_commit": "a" * 40,
            },
            "evidence": {
                "source": "slack_control_room",
                "authenticated": True,
                "slack_user_id": "U1234567",
                "slack_team_id": "T1234567",
                "slack_channel_id": "C1234567",
                "slack_event_id": "evt_auth",
                "github_repository": "gillella/Aru_Agentic_SDLC",
                "github_record_url": "https://github.com/gillella/Aru_Agentic_SDLC/issues/50#issuecomment-1",
                "recorded_at": "2026-08-17T10:00:00+00:00",
            },
            "scope_after": issue_scope,
        },
        {
            "decision": {
                "decision_id": f"{project_id}|T1234567|C1234567|evt_start",
                "action": "start",
                "increment_id": increment_id,
                "project_id": project_id,
                "operator_user_id": "U1234567",
                "github_repository": "gillella/Aru_Agentic_SDLC",
            },
            "evidence": {
                "source": "slack_control_room",
                "authenticated": True,
                "slack_user_id": "U1234567",
                "slack_team_id": "T1234567",
                "slack_channel_id": "C1234567",
                "slack_event_id": "evt_start",
                "github_repository": "gillella/Aru_Agentic_SDLC",
                "github_record_url": "https://github.com/gillella/Aru_Agentic_SDLC/issues/50#issuecomment-2",
                "recorded_at": "2026-08-17T11:00:00+00:00",
            },
            "scope_after": issue_scope,
        },
    ]

    if with_acceptance_evidence:
        record_url = (
            "http://insecure.example.com"
            if corrupt_evidence_url
            else "https://github.com/gillella/Aru_Agentic_SDLC/issues/50#issuecomment-3"
        )
        decisions.append({
            "decision": {
                "decision_id": f"{project_id}|T1234567|C1234567|evt_accept",
                "action": "accept",
                "increment_id": increment_id,
                "project_id": project_id,
                "operator_user_id": "U1234567",
                "github_repository": "gillella/Aru_Agentic_SDLC",
                "risk_accepted": False,
            },
            "evidence": {
                "source": "slack_control_room",
                "authenticated": authenticated,
                "slack_user_id": "U1234567",
                "slack_team_id": "T1234567",
                "slack_channel_id": "C1234567",
                "slack_event_id": "evt_accept",
                "github_repository": "gillella/Aru_Agentic_SDLC",
                "github_record_url": record_url,
                "recorded_at": "2026-08-17T12:00:00+00:00",
            },
            "scope_after": issue_scope,
        })

    return {
        "increment_id": increment_id,
        "project_id": project_id,
        "kind": "normal",
        "control_issue": 50,
        "issue_scope": issue_scope,
        "baseline_commit": "a" * 40,
        "lifecycle_state": lifecycle_state,
        "release_state": release_state,
        "created_at": "2026-08-17T10:00:00+00:00",
        "updated_at": "2026-08-17T12:00:00+00:00",
        "accepted_at": "2026-08-17T12:00:00+00:00" if lifecycle_state == "accepted" else None,
        "deployed_at": None,
        "decisions": decisions,
    }


class GitRepoTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_dir = Path(self.temp_dir.name)
        subprocess.run(["git", "init"], cwd=str(self.repo_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=str(self.repo_dir), check=True)
        subprocess.run(["git", "config", "user.email", "agent@test.local"], cwd=str(self.repo_dir), check=True)

        (self.repo_dir / "README.md").write_text("# Test Repo\n")
        subprocess.run(["git", "add", "."], cwd=str(self.repo_dir), check=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(self.repo_dir), check=True)

        res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(self.repo_dir), capture_output=True, text=True, check=True)
        self.head_commit = res.stdout.strip()

    def tearDown(self):
        self.temp_dir.cleanup()


class AcceptedCheckpointTests(GitRepoTestBase):
    def test_only_durably_accepted_increment_can_create_sprint_checkpoint(self):
        rec = make_sample_increment(lifecycle_state="accepted")
        res = create_checkpoint_tag(rec, self.head_commit, repo_path=self.repo_dir)
        self.assertTrue(res["created"])
        self.assertEqual(res["tag_name"], "ckpt/proj_sample/inc_0123456789abcdef0123")
        self.assertEqual(res["commit_sha"], self.head_commit)

    def test_unauthenticated_operator_evidence_refused(self):
        rec = make_sample_increment(lifecycle_state="accepted", authenticated=False)
        with self.assertRaises(IncrementReleaseError) as ctx:
            create_checkpoint_tag(rec, self.head_commit, repo_path=self.repo_dir)
        self.assertIn("authenticated operator evidence", str(ctx.exception))

    def test_corrupt_github_record_url_refused(self):
        rec = make_sample_increment(lifecycle_state="accepted", corrupt_evidence_url=True)
        with self.assertRaises(IncrementReleaseError) as ctx:
            create_checkpoint_tag(rec, self.head_commit, repo_path=self.repo_dir)
        self.assertIn("durable GitHub record URL", str(ctx.exception))


class TagIdentityTests(GitRepoTestBase):
    def test_annotated_tag_targets_exact_commit_and_includes_full_metadata(self):
        rec = make_sample_increment(
            increment_id="inc_abcdef0123456789abcd",
            project_id="proj_alpha",
            issue_scope=[201, 202, 203],
        )
        res = create_checkpoint_tag(rec, self.head_commit, repo_path=self.repo_dir)
        tag_name = res["tag_name"]
        self.assertEqual(tag_name, "ckpt/proj_alpha/inc_abcdef0123456789abcd")

        # Verify git object type is annotated tag
        cat_type = subprocess.run(["git", "cat-file", "-t", tag_name], cwd=str(self.repo_dir), capture_output=True, text=True, check=True)
        self.assertEqual(cat_type.stdout.strip(), "tag")

        # Verify tag content includes increment id, project id, issue set, decision URL, timestamp
        cat_tag = subprocess.run(["git", "cat-file", "-p", tag_name], cwd=str(self.repo_dir), capture_output=True, text=True, check=True)
        content = cat_tag.stdout
        self.assertIn("Increment: inc_abcdef0123456789abcd", content)
        self.assertIn("Project: proj_alpha", content)
        self.assertIn(f"Target-Commit: {self.head_commit}", content)
        self.assertIn("Committed-Issues: #201, #202, #203", content)
        self.assertIn("Decision-URL: https://github.com/gillella/Aru_Agentic_SDLC/issues/50#issuecomment-3", content)
        self.assertIn("Accepted-At: 2026-08-17T12:00:00+00:00", content)


class ReleaseRecordTests(GitRepoTestBase):
    def test_release_record_links_tag_evidence_demo_and_limitations(self):
        rec = make_sample_increment(
            increment_id="inc_11112222333344445555",
            project_id="proj_demo",
            issue_scope=[301],
            release_state="unreleased",
        )
        demo_artifacts = [{"name": "Preview Dashboard", "url": "https://gillella.github.io/demo/"}]
        limitations = ["Requires Postgres 16+ for JSONB indexing optimization"]

        outcome = publish_increment_release(
            record=rec,
            target_commit=self.head_commit,
            repo_path=self.repo_dir,
            demo_artifacts=demo_artifacts,
            limitations=limitations,
        )

        payload = outcome["release_record"]
        self.assertEqual(payload["tag_name"], "ckpt/proj_demo/inc_11112222333344445555")
        self.assertEqual(payload["target_commit"], self.head_commit)
        self.assertEqual(payload["deployment_state"], "unreleased")
        self.assertEqual(payload["issue_scope"], [301])
        self.assertEqual(len(payload["demo_artifacts"]), 1)
        self.assertEqual(payload["demo_artifacts"][0]["url"], "https://gillella.github.io/demo/")
        self.assertEqual(payload["limitations"], limitations)

        md = outcome["markdown"]
        self.assertIn("# Sprint Release Checkpoint: inc_11112222333344445555", md)
        self.assertIn("Issue #301", md)
        self.assertIn("Preview Dashboard", md)
        self.assertIn("Requires Postgres 16+", md)
        self.assertIn("Production deployment remains separate & held", md)


class IdempotencyTests(GitRepoTestBase):
    def test_idempotent_reinvocation_on_same_commit_succeeds(self):
        rec = make_sample_increment()
        res1 = create_checkpoint_tag(rec, self.head_commit, repo_path=self.repo_dir)
        self.assertTrue(res1["created"])
        self.assertFalse(res1.get("idempotent", False))

        # Reinvocation on same commit returns idempotent match
        res2 = create_checkpoint_tag(rec, self.head_commit, repo_path=self.repo_dir)
        self.assertFalse(res2["created"])
        self.assertTrue(res2["idempotent"])
        self.assertEqual(res2["commit_sha"], self.head_commit)

    def test_tag_pointing_to_different_commit_fails_closed(self):
        rec = make_sample_increment()
        create_checkpoint_tag(rec, self.head_commit, repo_path=self.repo_dir)

        # Make another commit
        (self.repo_dir / "OTHER.md").write_text("Different commit\n")
        subprocess.run(["git", "add", "."], cwd=str(self.repo_dir), check=True)
        subprocess.run(["git", "commit", "-m", "second commit"], cwd=str(self.repo_dir), check=True)
        second_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(self.repo_dir), capture_output=True, text=True, check=True
        ).stdout.strip()

        with self.assertRaises(IncrementReleaseError) as ctx:
            create_checkpoint_tag(rec, second_commit, repo_path=self.repo_dir)
        self.assertIn("already exists pointing to commit", str(ctx.exception))
        self.assertIn("which differs from requested commit", str(ctx.exception))


class DeploymentSeparationTests(GitRepoTestBase):
    def test_publishing_release_does_not_authorize_or_trigger_deployment(self):
        rec = make_sample_increment(release_state="unreleased")
        outcome = publish_increment_release(
            record=rec,
            target_commit=self.head_commit,
            repo_path=self.repo_dir,
        )
        record = outcome["release_record"]
        # Deployment state must remain as recorded, never promoted to deployment-authorized or deployed
        self.assertEqual(record["deployment_state"], "unreleased")
        self.assertEqual(rec["release_state"], "unreleased")
        self.assertIn("Production deployment remains separate & held", outcome["markdown"])


class CommandCheckpointTests(unittest.TestCase):
    @patch("delivery_increments.DeliveryIncrementStore")
    @patch.object(increment_release, "publish_increment_release")
    @patch.object(increment_release, "validate_release_checkpoint")
    def test_cli_validates_checkpoint_before_publish(self, validate, publish, store_cls):
        record = make_sample_increment()
        store_cls.return_value.list.return_value = [record]
        publish.return_value = {
            "tag_result": {"tag_name": "ckpt/p/i", "commit_sha": "a" * 40},
            "markdown": "release",
        }
        code = increment_release.main([
            "--increment", record["increment_id"],
            "--commit", "a" * 40,
            "--checkpoint-run-url", RUN_URL,
        ])
        self.assertEqual(code, 0)
        validate.assert_called_once_with(RUN_URL, "a" * 40)
        publish.assert_called_once()

    def test_cli_requires_checkpoint_run_url(self):
        with self.assertRaises(SystemExit):
            increment_release.main(["--increment", "inc_x", "--commit", "a" * 40])


class UnacceptedRefusalTests(GitRepoTestBase):
    def test_refuse_authorized_increment(self):
        rec = make_sample_increment(lifecycle_state="authorized", with_acceptance_evidence=False)
        with self.assertRaises(IncrementReleaseError) as ctx:
            create_checkpoint_tag(rec, self.head_commit, repo_path=self.repo_dir)
        self.assertIn("lifecycle_state is 'authorized'", str(ctx.exception))

    def test_refuse_active_increment(self):
        rec = make_sample_increment(lifecycle_state="active", with_acceptance_evidence=False)
        with self.assertRaises(IncrementReleaseError) as ctx:
            create_checkpoint_tag(rec, self.head_commit, repo_path=self.repo_dir)
        self.assertIn("lifecycle_state is 'active'", str(ctx.exception))

    def test_refuse_closed_increment(self):
        rec = make_sample_increment(lifecycle_state="closed", with_acceptance_evidence=False)
        with self.assertRaises(IncrementReleaseError) as ctx:
            create_checkpoint_tag(rec, self.head_commit, repo_path=self.repo_dir)
        self.assertIn("lifecycle_state is 'closed'", str(ctx.exception))

    def test_refuse_missing_acceptance_decision(self):
        rec = make_sample_increment(lifecycle_state="accepted", with_acceptance_evidence=False)
        with self.assertRaises(IncrementReleaseError) as ctx:
            create_checkpoint_tag(rec, self.head_commit, repo_path=self.repo_dir)
        self.assertIn("no durable 'accept' decision found", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
