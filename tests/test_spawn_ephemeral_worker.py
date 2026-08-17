import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import spawn_ephemeral_worker as sew


class EphemeralWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state_dir = self.root / "state"
        self.metadata = sew.PRMetadata(
            number=42,
            state="OPEN",
            head_name="feat/issue-42-safe-change",
            head_sha="a" * 40,
            author_agent="claude-1",
            author_family="anthropic",
            reviewer_agents=(),
        )

    def config(self, **overrides):
        values = {
            "repo": self.repo,
            "aru_home": ROOT,
            "pr": 42,
            "skill": sew.TASK_REVIEW,
            "parent_agent": "claude-1",
            "parent_family": "anthropic",
            "worker_agent": "codex-ephemeral-1",
            "worker_family": "openai",
            "adapter": "codex",
            "timeout": 30.0,
            "state_dir": self.state_dir,
        }
        values.update(overrides)
        return sew.LauncherConfig(**values)

    @staticmethod
    def registry_record(pid, worker_agent):
        return {
            "pid": pid,
            "repo": "/tmp/repo",
            "pr": pid,
            "skill": sew.TASK_REVIEW,
            "parent_agent": "parent",
            "worker_agent": worker_agent,
            "worker_family": "openai",
            "started_at": "2026-08-17T00:00:00Z",
        }

    def repo_with_pull_ref(self):
        origin = self.root / "origin.git"
        seed = self.root / "seed"
        checkout = self.root / "checkout"

        subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
        subprocess.run(["git", "init", "-b", "main", str(seed)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(seed), "config", "user.email", "tests@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(seed), "config", "user.name", "Aru Tests"],
            check=True,
        )
        (seed / "README.md").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(seed), "add", "README.md"], check=True)
        subprocess.run(
            ["git", "-C", str(seed), "commit", "-m", "fixture"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(seed), "push", str(origin), "main"],
            check=True,
            capture_output=True,
        )
        sha = subprocess.run(
            ["git", "-C", str(seed), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "--git-dir", str(origin), "update-ref", "refs/pull/42/head", sha],
            check=True,
        )
        subprocess.run(
            ["git", "clone", "--branch", "main", str(origin), str(checkout)],
            check=True,
            capture_output=True,
        )
        metadata = sew.PRMetadata(
            **{**self.metadata.__dict__, "head_sha": sha}
        )
        return checkout, metadata

    def test_registry_enforces_global_two_worker_ceiling(self):
        registry = sew.WorkerRegistry(self.state_dir, is_alive=lambda _pid: True)
        registry.register(self.registry_record(10, "worker-10"))
        registry.register(self.registry_record(11, "worker-11"))

        with self.assertRaisesRegex(sew.RegistryFull, r"limit \(2\) reached"):
            registry.register(self.registry_record(12, "worker-12"))

    def test_registry_reaps_dead_process_entries_before_counting(self):
        self.state_dir.mkdir()
        path = self.state_dir / "ephemeral-workers.json"
        path.write_text(json.dumps({
            "version": sew.REGISTRY_VERSION,
            "workers": [{**self.registry_record(99, "dead"), "token": "dead-token"}],
        }))
        registry = sew.WorkerRegistry(self.state_dir, is_alive=lambda pid: pid != 99)

        token = registry.register(self.registry_record(os.getpid(), "live"))

        workers = json.loads(path.read_text())["workers"]
        self.assertEqual([worker["worker_agent"] for worker in workers], ["live"])
        self.assertEqual(workers[0]["token"], token)

    def test_registry_rejects_duplicate_live_worker_identity(self):
        registry = sew.WorkerRegistry(self.state_dir, is_alive=lambda _pid: True)
        registry.register(self.registry_record(10, "worker-1"))

        with self.assertRaisesRegex(sew.RegistryFull, "already active"):
            registry.register(self.registry_record(11, "worker-1"))

    def test_same_family_peer_review_is_rejected(self):
        config = self.config(worker_family="anthropic")

        with self.assertRaisesRegex(sew.LauncherError, "worker_family != parent_family"):
            sew.validate_task(config, self.metadata)

    def test_review_worker_cannot_share_author_family(self):
        config = self.config(parent_family="google", worker_family="anthropic")

        with self.assertRaisesRegex(sew.LauncherError, "distinct from the PR author"):
            sew.validate_task(config, self.metadata)

    def test_feedback_worker_must_match_stamped_author(self):
        config = self.config(
            skill=sew.TASK_FEEDBACK,
            worker_agent="codex-1",
            worker_family="openai",
        )

        with self.assertRaisesRegex(sew.LauncherError, "stamped author identity"):
            sew.validate_task(config, self.metadata)

    def test_recursive_spawn_fails_before_github_lookup(self):
        with (
            patch.dict(os.environ, {"ARU_CAN_SPAWN": "0"}),
            patch.object(sew, "load_pr_metadata") as metadata,
        ):
            with self.assertRaisesRegex(sew.LauncherError, "recursive ephemeral"):
                sew.execute(self.config())

        metadata.assert_not_called()

    def test_child_environment_disables_recursive_spawning(self):
        with patch.dict(os.environ, {
            "GIT_DIR": "/tmp/wrong.git",
            "GIT_WORK_TREE": "/tmp/wrong-tree",
            "GH_REPO": "attacker/wrong",
            "GH_HOST": "wrong.invalid",
            "GH_TOKEN": "preserved-test-token",
        }):
            environment = sew.child_environment(self.config())

        self.assertEqual(environment["ARU_CAN_SPAWN"], "0")
        self.assertEqual(environment["ARU_EPHEMERAL"], "1")
        self.assertEqual(environment["ARU_AGENT_ID"], "codex-ephemeral-1")
        self.assertEqual(environment["ARU_EPHEMERAL_PR"], "42")
        self.assertEqual(environment["ARU_EPHEMERAL_SKILL"], sew.TASK_REVIEW)
        self.assertEqual(environment["GIT_NO_REPLACE_OBJECTS"], "1")
        self.assertEqual(environment["GH_TOKEN"], "preserved-test-token")
        self.assertNotIn("GIT_DIR", environment)
        self.assertNotIn("GIT_WORK_TREE", environment)
        self.assertNotIn("GH_REPO", environment)
        self.assertNotIn("GH_HOST", environment)

    def test_pr_read_targets_slug_derived_from_origin(self):
        payload = {
            "number": 42,
            "state": "OPEN",
            "headRefName": self.metadata.head_name,
            "headRefOid": self.metadata.head_sha,
            "isCrossRepository": False,
            "labels": [
                {"name": "author:claude-1"},
                {"name": "family:anthropic"},
            ],
        }
        with (
            patch.object(sew, "repository_slug", return_value="owner/repo"),
            patch.object(
                sew,
                "run_authority_command",
                return_value=(0, json.dumps(payload), ""),
            ) as command,
        ):
            metadata = sew.load_pr_metadata(self.repo, 42)

        self.assertEqual(metadata, self.metadata)
        self.assertIn("--repo", command.call_args.args[0])
        repo_index = command.call_args.args[0].index("--repo")
        self.assertEqual(command.call_args.args[0][repo_index + 1], "owner/repo")

    def test_timeout_terminates_then_kills_worker_process_group(self):
        process = Mock(pid=4321)
        process.wait.side_effect = [
            subprocess.TimeoutExpired(["agent"], 5),
            subprocess.TimeoutExpired(["agent"], 2),
            124,
        ]
        with (
            patch.object(sew.subprocess, "Popen", return_value=process) as spawn,
            patch.object(sew.os, "killpg") as kill_group,
        ):
            result = sew.run_worker(["agent", "prompt"], self.repo, {}, 5)

        self.assertEqual(result, sew.WorkerResult(124, timed_out=True))
        spawn.assert_called_once_with(
            ["agent", "prompt"],
            cwd=str(self.repo),
            env={},
            start_new_session=True,
        )
        self.assertEqual(
            kill_group.call_args_list,
            [call(4321, signal) for signal in (sew.signal.SIGTERM, sew.signal.SIGKILL)],
        )

    def test_nonzero_review_worker_releases_claim_and_cleans_worktree(self):
        worktree = self.repo / ".worktrees" / f"ephemeral-review-{os.getpid()}"
        with (
            patch.object(sew, "load_pr_metadata", return_value=self.metadata),
            patch.object(sew, "claim_review", return_value=0),
            patch.object(sew, "prepare_worktree", return_value=(worktree, "")),
            patch.object(sew, "build_agent_argv", return_value=["agent"]),
            patch.object(sew.shutil, "which", return_value="/bin/agent"),
            patch.object(sew, "run_worker", return_value=sew.WorkerResult(9)) as run,
            patch.object(sew, "release_review", return_value=0) as release,
            patch.object(sew, "cleanup_worktree", return_value=True) as cleanup,
        ):
            code = sew.execute(self.config())

        self.assertEqual(code, 9)
        self.assertEqual(run.call_args.args[2]["ARU_CAN_SPAWN"], "0")
        release.assert_called_once_with(self.config())
        cleanup.assert_called_once_with(self.repo, worktree, "")
        registry = json.loads((self.state_dir / "ephemeral-workers.json").read_text())
        self.assertEqual(registry["workers"], [])

    def test_clean_review_exit_requires_worker_to_release_claim(self):
        claimed = sew.PRMetadata(
            **{**self.metadata.__dict__, "reviewer_agents": ("codex-ephemeral-1",)}
        )
        worktree = self.repo / ".worktrees" / f"ephemeral-review-{os.getpid()}"
        with (
            patch.object(sew, "load_pr_metadata", side_effect=[self.metadata, claimed]),
            patch.object(sew, "claim_review", return_value=0),
            patch.object(sew, "prepare_worktree", return_value=(worktree, "")),
            patch.object(sew, "build_agent_argv", return_value=["agent"]),
            patch.object(sew.shutil, "which", return_value="/bin/agent"),
            patch.object(sew, "run_worker", return_value=sew.WorkerResult(0)),
            patch.object(sew, "release_review", return_value=0) as release,
            patch.object(sew, "cleanup_worktree", return_value=True),
        ):
            code = sew.execute(self.config())

        self.assertEqual(code, 1)
        release.assert_called_once_with(self.config())

    def test_clean_exit_unregisters_and_cleans_owned_worktree(self):
        worktree = self.repo / ".worktrees" / f"ephemeral-review-{os.getpid()}"
        with (
            patch.object(
                sew, "load_pr_metadata", side_effect=[self.metadata, self.metadata]
            ),
            patch.object(sew, "claim_review", return_value=0),
            patch.object(sew, "prepare_worktree", return_value=(worktree, "")),
            patch.object(sew, "build_agent_argv", return_value=["agent"]),
            patch.object(sew.shutil, "which", return_value="/bin/agent"),
            patch.object(sew, "run_worker", return_value=sew.WorkerResult(0)),
            patch.object(sew, "cleanup_worktree", return_value=True) as cleanup,
        ):
            code = sew.execute(self.config())

        self.assertEqual(code, 0)
        cleanup.assert_called_once_with(self.repo, worktree, "")
        registry = json.loads((self.state_dir / "ephemeral-workers.json").read_text())
        self.assertEqual(registry["workers"], [])

    def test_missing_cli_fails_closed_releases_claim_and_cleans(self):
        worktree = self.repo / ".worktrees" / f"ephemeral-review-{os.getpid()}"
        with (
            patch.object(sew, "load_pr_metadata", return_value=self.metadata),
            patch.object(sew, "claim_review", return_value=0),
            patch.object(sew, "prepare_worktree", return_value=(worktree, "")),
            patch.object(sew, "build_agent_argv", return_value=["missing-agent"]),
            patch.object(sew.shutil, "which", return_value=None),
            patch.object(sew, "release_review", return_value=0) as release,
            patch.object(sew, "cleanup_worktree", return_value=True) as cleanup,
        ):
            code = sew.execute(self.config())

        self.assertEqual(code, 1)
        release.assert_called_once_with(self.config())
        cleanup.assert_called_once_with(self.repo, worktree, "")

    def test_cleanup_refuses_non_ephemeral_path(self):
        with patch.object(sew, "run_authority_command") as command:
            ok = sew.cleanup_worktree(self.repo, self.repo / "important", "")

        self.assertFalse(ok)
        command.assert_not_called()

    def test_review_worktree_is_pinned_to_pull_head_and_removed(self):
        repo, metadata = self.repo_with_pull_ref()

        path, branch = sew.prepare_worktree(
            repo, metadata, sew.TASK_REVIEW, 12345
        )

        head = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(head, metadata.head_sha)
        self.assertEqual(branch, "")
        self.assertTrue(sew.cleanup_worktree(repo, path, branch))
        self.assertFalse(path.exists())

    def test_feedback_cleanup_removes_launcher_owned_branch(self):
        repo, metadata = self.repo_with_pull_ref()

        path, branch = sew.prepare_worktree(
            repo, metadata, sew.TASK_FEEDBACK, 12346
        )

        self.assertTrue(branch.startswith("ephemeral/feedback-pr-42-"))
        self.assertTrue(sew.cleanup_worktree(repo, path, branch))
        branch_check = subprocess.run(
            ["git", "-C", str(repo), "show-ref", "--verify", f"refs/heads/{branch}"],
            capture_output=True,
        )
        self.assertNotEqual(branch_check.returncode, 0)


if __name__ == "__main__":
    unittest.main()
