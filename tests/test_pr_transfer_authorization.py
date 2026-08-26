"""Trusted per-transfer authorization contract for immediate PR adoption."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import pr_transfer_authorization as auth  # noqa: E402


class TransferAuthorizationTests(unittest.TestCase):
    def comment(self, **overrides):
        item = {"body": auth.render_transfer_approval(
                    "agent-2", "openai", "a" * 40, "Author unavailable"),
                "user": {"login": "operator", "type": "User"}}
        item.update(overrides)
        return item

    def approved(self, comments, permission=(0, "write\n", "")):
        with patch.object(auth, "get_repo_slug", return_value="acme/widgets"), \
             patch.object(auth, "fetch_issue_comments", return_value=comments), \
             patch.object(auth, "run_cmd", return_value=permission) as run:
            result = auth.operator_transfer_approved(
                42, "agent-2", "openai", "a" * 40, "Author unavailable")
        return result, run

    def test_exact_write_capable_approval_is_accepted(self):
        result, run = self.approved([self.comment()])
        self.assertTrue(result)
        self.assertIn("collaborators/operator/permission", run.call_args.args[0][2])

    def test_approval_is_bound_to_every_transfer_field(self):
        for changed in ("agent", "family", "head", "reason"):
            with self.subTest(changed=changed):
                body = auth.render_transfer_approval(
                    "other" if changed == "agent" else "agent-2",
                    "google" if changed == "family" else "openai",
                    "b" * 40 if changed == "head" else "a" * 40,
                    "Other" if changed == "reason" else "Author unavailable")
                result, run = self.approved([self.comment(body=body)])
                self.assertFalse(result)
                run.assert_not_called()

    def test_bot_or_read_only_actor_cannot_authorize(self):
        result, run = self.approved([
            self.comment(user={"login": "app[bot]", "type": "Bot"})])
        self.assertFalse(result)
        run.assert_not_called()
        self.assertFalse(self.approved([self.comment()], (0, "read\n", ""))[0])

    def test_unreadable_permission_or_comment_history_fails_closed(self):
        self.assertFalse(self.approved([self.comment()], (1, "", "denied"))[0])
        self.assertFalse(self.approved([])[0])


if __name__ == "__main__":
    unittest.main()
