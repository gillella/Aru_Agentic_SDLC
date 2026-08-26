# line-ceiling: 150
"""Owner, expiry, and compare-and-swap behavior for reassignment leases."""

import sys
import unittest
from pathlib import Path
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import review_reassignment_lock as lock  # noqa: E402
import reassign_review as rr  # noqa: E402

OURS = "a" * 40
OLDER = "b" * 40
HEAD = "c" * 40
REF = f"{lock.LOCK_REF_PREFIX}433"


class RemoteReassignmentLockTests(unittest.TestCase):
    def acquire(self, pushes, remote=None, now=1000):
        with patch.object(lock, "_new_lock_blob", return_value=(OURS, None)), \
             patch.object(lock, "_push_lock", side_effect=pushes) as push, \
             patch.object(lock, "_remote_lock", return_value=(remote, None)), \
             patch.object(lock.time, "time", return_value=now):
            with lock.remote_reassignment_lock(433, HEAD) as acquired:
                result = acquired
        return result, push

    def test_active_owner_blocks_without_deleting_its_ref(self):
        result, push = self.acquire(
            [(1, "", "exists")], (OLDER, {"expires_at": 1100, "owner": "peer"}))
        self.assertFalse(result)
        push.assert_called_once_with(REF, OURS, "")

    def test_expired_owner_is_recovered_with_compare_and_swap(self):
        result, push = self.acquire(
            [(1, "", "exists"), (0, "", ""), (0, "", ""), (0, "", "")],
            (OLDER, {"expires_at": 999, "owner": "peer"}))
        self.assertTrue(result)
        self.assertEqual(push.call_args_list, [
            call(REF, OURS, ""), call(REF, "", OLDER),
            call(REF, OURS, ""), call(REF, "", OURS)])

    def test_lost_stale_delete_never_removes_or_replaces_new_owner(self):
        result, push = self.acquire(
            [(1, "", "exists"), (1, "", "lease mismatch")],
            (OLDER, {"expires_at": 999, "owner": "peer"}))
        self.assertFalse(result)
        self.assertEqual(push.call_args_list,
                         [call(REF, OURS, ""), call(REF, "", OLDER)])

    def test_disappeared_contender_allows_one_absent_ref_retry(self):
        result, push = self.acquire(
            [(1, "", "exists"), (0, "", ""), (0, "", "")], None)
        self.assertTrue(result)
        self.assertEqual(push.call_args_list,
                         [call(REF, OURS, ""), call(REF, OURS, ""),
                          call(REF, "", OURS)])

    def test_unknown_or_legacy_lock_shape_fails_closed(self):
        with patch.object(lock, "_new_lock_blob", return_value=(OURS, None)), \
             patch.object(lock, "_push_lock", return_value=(1, "", "exists")), \
             patch.object(lock, "_remote_lock", return_value=(None, "invalid lease")):
            with lock.remote_reassignment_lock(433, HEAD) as acquired:
                self.assertFalse(acquired)


class ReviewTriggerAuthorizationTests(unittest.TestCase):
    @patch.object(lock, "get_repo_slug", return_value="acme/widgets")
    @patch.object(lock, "run_cmd", return_value=(0, "write\n", ""))
    def test_write_permission_is_authorized(self, run, _slug):
        self.assertTrue(lock.review_trigger_authorized("alice"))
        self.assertIn("collaborators/alice/permission", run.call_args.args[0][2])

    @patch.object(lock, "get_repo_slug", return_value="acme/widgets")
    @patch.object(lock, "run_cmd", return_value=(0, "read\n", ""))
    def test_read_only_commenter_cannot_suppress_trigger(self, _run, _slug):
        self.assertFalse(lock.review_trigger_authorized("reader"))

    @patch.object(lock, "get_repo_slug", return_value="acme/widgets")
    @patch.object(lock, "run_cmd", return_value=(1, "", "denied"))
    def test_unreadable_permission_fails_closed(self, _run, _slug):
        self.assertIsNone(lock.review_trigger_authorized("unknown"))


class HistorySettlementTests(unittest.TestCase):
    def test_eventually_consistent_audit_visibility_is_retried(self):
        prior = []
        expected = [{"from": "review:coderabbit", "to": "review:sourcery"}]
        with patch.object(rr, "reassignment_history",
                          side_effect=[prior, prior, expected]), \
             patch.object(rr.time, "sleep") as sleep:
            committed = rr._settled_history(433, prior, expected)
        self.assertEqual(committed, expected)
        self.assertEqual(sleep.call_count, 2)

    def test_rival_audit_does_not_wait_or_masquerade_as_ours(self):
        prior = []
        expected = [{"from": "review:coderabbit", "to": "review:sourcery"}]
        rival = [{"from": "review:coderabbit", "to": "review:codeant"}]
        with patch.object(rr, "reassignment_history", return_value=rival), \
             patch.object(rr.time, "sleep") as sleep:
            committed = rr._settled_history(433, prior, expected)
        self.assertEqual(committed, rival)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
