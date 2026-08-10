import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

import enforce_touches as et


class TouchesParsingTests(unittest.TestCase):
    def test_reads_the_metadata_line_and_ignores_prose(self):
        body = """## Summary

The `touches:` contract prevents collisions between agents.

## Dependencies

depends-on: #12
touches: src/api/*, docs/SPEC.md
parallel-eligible: true
"""
        self.assertEqual(et.parse_touches(body), ["src/api/*", "docs/SPEC.md"])

    def test_tolerates_markdown_emphasis_around_the_key(self):
        self.assertEqual(et.parse_touches("**touches:** a.py, b.py"), ["a.py", "b.py"])

    def test_prose_placeholder_is_not_treated_as_a_path(self):
        # "(github settings only)" means the issue changes nothing in the tree.
        # Reading it as a literal path would block every write on the branch.
        self.assertEqual(et.parse_touches("touches: (github settings only)"), [])

    def test_missing_declaration_is_empty(self):
        self.assertEqual(et.parse_touches("## Summary\n\nNo metadata here."), [])


class PathAllowanceTests(unittest.TestCase):
    def test_exact_file_match(self):
        self.assertTrue(et.path_allowed("pyproject.toml", ["pyproject.toml"]))

    def test_directory_entry_covers_children(self):
        self.assertTrue(et.path_allowed("docs/a/b.md", ["docs"]))
        self.assertTrue(et.path_allowed("docs/a/b.md", ["docs/"]))

    def test_glob_covers_nested_paths(self):
        # "src/api/*" is written by humans meaning "everything under src/api".
        self.assertTrue(et.path_allowed("src/api/handlers/user.py", ["src/api/*"]))
        self.assertTrue(et.path_allowed("src/api/user.py", ["src/api/*"]))

    def test_unrelated_path_is_denied(self):
        self.assertFalse(et.path_allowed("src/core/db.py", ["src/api/*", "docs/SPEC.md"]))

    def test_sibling_prefix_is_not_a_match(self):
        # "docs" must not cover "docs-internal".
        self.assertFalse(et.path_allowed("docs-internal/x.md", ["docs"]))


class BranchParsingTests(unittest.TestCase):
    def test_governed_branch_yields_issue_number(self):
        self.assertEqual(et.issue_from_branch("feat/issue-42-add-auth"), 42)
        self.assertEqual(et.issue_from_branch("chore/issue-7-ci"), 7)

    def test_nonconforming_branch_yields_none(self):
        # This is the real-world case that motivated the hook: a branch named
        # this way is invisible to the picker's resume logic too.
        self.assertIsNone(et.issue_from_branch("docs/30-current-state-gap-analysis"))
        self.assertIsNone(et.issue_from_branch("main"))


class ProtectedBranchTests(unittest.TestCase):
    def test_commit_on_main_is_blocked(self):
        self.assertIsNotNone(et._git_write_to_protected("git commit -m 'x'", "main"))

    def test_commit_on_feature_branch_is_fine(self):
        self.assertIsNone(et._git_write_to_protected("git commit -m 'x'", "feat/issue-1-a"))

    def test_explicit_push_to_main_is_blocked_from_any_branch(self):
        self.assertIsNotNone(et._git_write_to_protected("git push origin main", "feat/issue-1-a"))
        self.assertIsNotNone(et._git_write_to_protected("git push origin HEAD:main", "feat/issue-1-a"))
        self.assertIsNotNone(
            et._git_write_to_protected("git push origin refs/heads/master", "feat/issue-1-a")
        )

    def test_quoted_ref_is_still_detected(self):
        self.assertIsNotNone(et._git_write_to_protected('git push origin "main"', "feat/issue-1-a"))

    def test_bare_push_while_on_main_is_blocked(self):
        self.assertIsNotNone(et._git_write_to_protected("git push", "main"))

    def test_bare_push_on_feature_branch_is_allowed(self):
        self.assertIsNone(et._git_write_to_protected("git push -u origin HEAD", "feat/issue-1-a"))

    def test_unrelated_command_is_allowed(self):
        self.assertIsNone(et._git_write_to_protected("pytest -q", "main"))


class RedirectDetectionTests(unittest.TestCase):
    def test_finds_redirect_and_tee_and_sed_targets(self):
        self.assertIn("out.txt", et._redirect_targets("echo hi > out.txt"))
        self.assertIn("out.txt", et._redirect_targets("echo hi >> out.txt"))
        self.assertIn("conf.yml", et._redirect_targets("cat x | tee conf.yml"))
        self.assertIn("file.py", et._redirect_targets("sed -i '' 's/a/b/' file.py"))

    def test_pipe_without_write_yields_nothing(self):
        self.assertEqual(et._redirect_targets("grep -n foo bar.py | head"), [])

    def test_fd_duplication_is_not_a_write(self):
        # 2>&1 and >&2 rebind a descriptor; nothing is created on disk.
        self.assertEqual(et._redirect_targets("cmd 2>&1"), [])
        self.assertEqual(et._redirect_targets("cmd >&2"), [])

    def test_redirect_target_glued_to_the_operator_is_found(self):
        self.assertIn("out.txt", et._redirect_targets("echo hi >out.txt"))
        self.assertIn("err.log", et._redirect_targets("cmd 2> err.log"))

    def test_operator_glued_to_the_preceding_word_is_still_a_write(self):
        """No token starts with the operator here, yet all three write.

        A lexer without punctuation_chars leaves 'hi>out.txt' whole and the
        write vanishes. That is the dangerous direction: a missed write lets
        an agent edit outside its declaration, which is the whole point of
        the hook.
        """
        self.assertIn("out.txt", et._redirect_targets("echo hi>out.txt"))
        self.assertIn("out.txt", et._redirect_targets("echo hi>>out.txt"))
        self.assertIn("out.txt", et._redirect_targets("echo hi &> out.txt"))

    def test_quoted_target_containing_a_space_is_read_whole(self):
        # The old character-class scan stopped at the space and reported '"my'.
        self.assertEqual(et._redirect_targets('echo hi > "my file.txt"'), ["my file.txt"])


class RedirectFalsePositiveTests(unittest.TestCase):
    """Prose containing '>' is not a write.

    Every case here blocked a real commit before the lexer replaced the regex
    scan. The module's contract is to block only on positive proof of a write,
    so a '>' inside a quoted argument must never count: the agent's only
    escapes are to mangle the message or to widen touches: past what it
    actually writes, and both dissolve the guarantee the hook exists to give.
    """

    TRAILER = 'Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>'

    def test_ascii_arrow_in_a_message_is_not_a_redirect(self):
        self.assertEqual(
            et._redirect_targets('git commit -m "tests present -> run them"'), []
        )

    def test_fat_arrow_is_not_a_redirect(self):
        self.assertEqual(et._redirect_targets('echo "a => b"'), [])

    def test_comparison_in_prose_is_not_a_redirect(self):
        self.assertEqual(et._redirect_targets('echo "if x > y then"'), [])

    def test_co_authored_by_trailer_via_m_flag_is_not_a_redirect(self):
        # The mandated trailer in docs/coding_standards.md ends in '>'.
        # Reading it as a redirect blocked every conforming commit.
        self.assertEqual(
            et._redirect_targets(f'git commit -m "msg" -m "{self.TRAILER}"'), []
        )

    def test_co_authored_by_trailer_in_a_heredoc_is_not_a_redirect(self):
        # '\\s*' used to span the newline, so the trailing '>' swallowed the
        # heredoc terminator and the hook reported a write to 'EOF'.
        command = "git commit -F - <<'EOF'\nsubject\n\n" + self.TRAILER + "\nEOF"
        self.assertEqual(et._redirect_targets(command), [])

    def test_quoted_argument_beginning_with_the_operator_is_not_a_redirect(self):
        """The operator is the first character *inside* the quotes.

        Lexing with shlex.split was not enough: it discards quoting, so this
        arrived as a token starting with '>' and was indistinguishable from a
        real redirect. punctuation_chars keeps a genuine operator standalone,
        so an operator with content attached can only have come from quotes.
        """
        self.assertEqual(et._redirect_targets('git commit -m "> fix parser"'), [])
        self.assertEqual(et._redirect_targets('git commit -m ">"'), [])
        self.assertEqual(et._redirect_targets('echo ">> appending"'), [])

    def test_heredoc_body_is_data_but_a_redirect_beside_it_still_counts(self):
        """Stripping the body must not swallow the rest of the opener line.

        A first attempt consumed from the delimiter to the terminator, which
        hid the `> out.txt` in `cat <<'EOF' > out.txt` - turning a fix for
        false positives into a false negative, the more dangerous direction.
        """
        command = "cat <<'EOF' > out.txt\nbody line\nEOF"
        self.assertEqual(et._redirect_targets(command), ["out.txt"])

    def test_unlexable_command_fails_open(self):
        # Unbalanced quotes must not block; the contract is fail-open.
        self.assertEqual(et._redirect_targets('echo "unterminated'), [])

    def test_empty_target_is_never_reported(self):
        self.assertNotIn("", et._redirect_targets('git commit -m "trailing >"'))

    def test_issue_body_documenting_redirects_is_not_a_redirect(self):
        # Filing the bug report was itself blocked: the body had to quote the
        # offending characters to describe them.
        body = "| `a -> b` | `x => y` | `if p > q` |"
        self.assertEqual(
            et._redirect_targets(f'gh issue create --title t --body "{body}"'), []
        )


class PostPr23ParserGapTests(unittest.TestCase):
    """The gaps found on #23's merged head, each checked against real Bash.

    Every expectation here was taken from running the command in a scratch
    directory and listing what appeared, not from reading the parser. Two
    directions of failure, and they are not equally bad: a false positive
    blocks legitimate work and is visible immediately, while a false negative
    lets a write escape the touches budget silently.
    """

    def test_quoted_operator_followed_by_an_argument_is_not_a_redirect(self):
        # `echo ">" file.txt` prints two arguments and writes nothing.
        self.assertEqual(et._redirect_targets('echo ">" file.txt'), [])
        self.assertEqual(et._redirect_targets("echo '>' file.txt"), [])
        self.assertEqual(et._redirect_targets('echo ">>" file.txt'), [])

    def test_escaped_operator_followed_by_an_argument_is_not_a_redirect(self):
        # Backslash escapes survive lexing only as trailing backslashes on the
        # preceding token; the operator token itself looks entirely ordinary.
        self.assertEqual(et._redirect_targets(r"echo \> file.txt"), [])
        self.assertEqual(et._redirect_targets(r"echo \>\> file.txt"), [])
        self.assertEqual(et._redirect_targets(r"echo a\>b"), [])

    def test_an_even_run_of_backslashes_does_not_escape_the_operator(self):
        # `echo \\ > f.txt` prints a literal backslash and really does redirect.
        # Treating this as escaped would be a false negative.
        self.assertEqual(et._redirect_targets(r"echo \\ > realbs.txt"), ["realbs.txt"])

    def test_heredoc_delimiter_may_contain_shell_safe_punctuation(self):
        # `\w+` did not match END-MSG, so the body was never stripped and the
        # '>' closing the trailer address lexed as a redirect onto the
        # terminator. This is the case that blocked committing this very fix.
        command = (
            'git commit -F - <<"END-MSG"\n'
            "fix: something\n\n"
            "Co-Authored-By: Claude <noreply@anthropic.com>\n"
            "END-MSG\n"
        )
        self.assertEqual(et._redirect_targets(command), [])

    def test_a_real_redirect_on_the_heredoc_opener_survives_body_stripping(self):
        # Stripping the body must not cost the redirect beside it; that would
        # trade a false positive for the worse failure.
        command = "cat <<'END-MSG' > out.txt\nbody\nEND-MSG\n"
        self.assertEqual(et._redirect_targets(command), ["out.txt"])

    def test_dup_operator_writes_a_file_when_the_target_is_not_a_descriptor(self):
        # The dangerous one. `echo hi >&out.txt` creates or truncates out.txt,
        # and classifying >& as duplication unconditionally hid it entirely.
        self.assertEqual(et._redirect_targets("echo hi >&out.txt"), ["out.txt"])

    def test_dup_operator_with_a_descriptor_target_is_not_a_write(self):
        for command in ("echo hi >&2", "echo hi 2>&1", "echo hi >&-"):
            with self.subTest(command=command):
                self.assertEqual(et._redirect_targets(command), [])


class HookDecisionTests(unittest.TestCase):
    """End-to-end main() behaviour with GitHub and git stubbed out."""

    def _run(self, payload, branch, touches, tool="Edit"):
        with patch.object(et.sys, "stdin", io.StringIO(json.dumps(payload))), \
             patch.object(et, "repo_root", return_value="/repo"), \
             patch.object(et, "current_branch", return_value=branch), \
             patch.object(et, "touches_for", return_value=touches):
            return et.main()

    def test_write_inside_declaration_is_allowed(self):
        rc = self._run(
            {"tool_name": "Edit", "tool_input": {"file_path": "/repo/src/api/x.py"}, "cwd": "/repo"},
            "feat/issue-9-api", ["src/api/*"],
        )
        self.assertEqual(rc, et.EXIT_ALLOW)

    def test_write_outside_declaration_is_blocked(self):
        rc = self._run(
            {"tool_name": "Edit", "tool_input": {"file_path": "/repo/pyproject.toml"}, "cwd": "/repo"},
            "feat/issue-9-api", ["src/api/*"],
        )
        self.assertEqual(rc, et.EXIT_BLOCK)

    def test_ungoverned_branch_fails_open(self):
        rc = self._run(
            {"tool_name": "Edit", "tool_input": {"file_path": "/repo/anything.py"}, "cwd": "/repo"},
            "scratch/experiment", ["src/api/*"],
        )
        self.assertEqual(rc, et.EXIT_ALLOW)

    def test_unreadable_issue_fails_open(self):
        # A GitHub outage must not halt the session.
        rc = self._run(
            {"tool_name": "Edit", "tool_input": {"file_path": "/repo/anything.py"}, "cwd": "/repo"},
            "feat/issue-9-api", None,
        )
        self.assertEqual(rc, et.EXIT_ALLOW)

    def test_path_outside_the_repo_is_not_governed(self):
        rc = self._run(
            {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/scratch.py"}, "cwd": "/repo"},
            "feat/issue-9-api", ["src/api/*"],
        )
        self.assertEqual(rc, et.EXIT_ALLOW)

    def test_read_only_tool_is_never_blocked(self):
        rc = self._run(
            {"tool_name": "Read", "tool_input": {"file_path": "/repo/pyproject.toml"}, "cwd": "/repo"},
            "feat/issue-9-api", ["src/api/*"],
        )
        self.assertEqual(rc, et.EXIT_ALLOW)

    def test_shell_redirect_outside_declaration_is_blocked(self):
        rc = self._run(
            {"tool_name": "Bash", "tool_input": {"command": "echo x > /repo/pyproject.toml"}, "cwd": "/repo"},
            "feat/issue-9-api", ["src/api/*"],
        )
        self.assertEqual(rc, et.EXIT_BLOCK)

    def test_dup_operator_write_outside_declaration_is_blocked(self):
        # The bypass, end to end: `>&` reaches the filesystem exactly like `>`,
        # so the hook must refuse it outside the budget. Before the fix this
        # returned EXIT_ALLOW and the write landed unrecorded.
        rc = self._run(
            {"tool_name": "Bash",
             "tool_input": {"command": "echo x >&/repo/pyproject.toml"}, "cwd": "/repo"},
            "feat/issue-9-api", ["src/api/*"],
        )
        self.assertEqual(rc, et.EXIT_BLOCK)

    def test_quoted_operator_in_a_commit_message_is_not_blocked(self):
        # The other direction: legitimate work must not be refused.
        rc = self._run(
            {"tool_name": "Bash",
             "tool_input": {"command": 'git commit -m "use \\">\\" for redirects"'}, "cwd": "/repo"},
            "feat/issue-9-api", ["src/api/*"],
        )
        self.assertEqual(rc, et.EXIT_ALLOW)

    def test_push_to_main_blocked_even_without_a_claim(self):
        rc = self._run(
            {"tool_name": "Bash", "tool_input": {"command": "git push origin main"}, "cwd": "/repo"},
            "scratch/experiment", None,
        )
        self.assertEqual(rc, et.EXIT_BLOCK)

    def test_malformed_stdin_fails_open(self):
        with patch.object(et.sys, "stdin", io.StringIO("not json")):
            self.assertEqual(et.main(), et.EXIT_ALLOW)

    def test_outside_a_git_repo_fails_open(self):
        with patch.object(et.sys, "stdin", io.StringIO(json.dumps(
                {"tool_name": "Edit", "tool_input": {"file_path": "x.py"}, "cwd": "/nope"}))), \
             patch.object(et, "repo_root", return_value=None):
            self.assertEqual(et.main(), et.EXIT_ALLOW)


if __name__ == "__main__":
    unittest.main()
