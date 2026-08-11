import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import mock_open, patch

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


class GovernedRepoTests(unittest.TestCase):
    @patch.object(et.os.path, "isfile", return_value=True)
    def test_issue_first_marker_enables_governance(self, _isfile):
        with patch("builtins.open", mock_open(
                read_data="# Core Governance: The Issue-First Law\n")):
            self.assertTrue(et.governed_repo("/repo"))

    @patch.object(et.os.path, "isfile", return_value=True)
    def test_generated_emoji_heading_enables_governance(self, _isfile):
        with patch("builtins.open", mock_open(
                read_data="## 🚨 Core Governance: The Issue-First Law\n")):
            self.assertTrue(et.governed_repo("/repo"))

    @patch.object(et.os.path, "isfile", return_value=True)
    def test_unrelated_agents_file_is_not_aru_governance(self, _isfile):
        with patch("builtins.open", mock_open(
                read_data="# Local development notes\n")):
            self.assertFalse(et.governed_repo("/repo"))

    @patch.object(et.os.path, "isfile", return_value=True)
    def test_negated_or_comparison_mentions_do_not_enable_governance(self, _isfile):
        for text in (
            "This repository does not use the Issue-First Law.\n",
            "Compare against the Issue-First Law in another project.\n",
            "## Notes about the Issue-First Law\n",
        ):
            with self.subTest(text=text), patch("builtins.open", mock_open(read_data=text)):
                self.assertFalse(et.governed_repo("/repo"))

    @patch.object(et.os.path, "isfile", return_value=False)
    def test_missing_agents_file_is_ungoverned(self, _isfile):
        self.assertFalse(et.governed_repo("/repo"))

    @patch.object(et.os.path, "isfile", return_value=True)
    def test_unreadable_agents_file_is_unknown(self, _isfile):
        with patch("builtins.open", side_effect=OSError("denied")):
            self.assertIsNone(et.governed_repo("/repo"))


class RealPathNormalizationTests(unittest.TestCase):
    @staticmethod
    def _case_insensitive_samefile(left, right):
        return os.fspath(left).lower() == os.fspath(right).lower()

    def test_component_named_dot_dot_prefix_is_inside(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "repo"
            root.mkdir()
            self.assertEqual(et._norm(root / "..evil", str(root)), "..evil")

    def test_external_symlink_alias_into_repo_is_governed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "repo"
            root.mkdir()
            alias = Path(temp_dir) / "repo-alias"
            alias.symlink_to(root, target_is_directory=True)
            self.assertEqual(et._norm(alias / "README.md", str(root)), "README.md")

    def test_internal_symlink_alias_outside_repo_is_not_governed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "repo"
            outside = Path(temp_dir) / "outside"
            root.mkdir()
            outside.mkdir()
            (root / "alias-out").symlink_to(outside, target_is_directory=True)
            self.assertIsNone(et._norm(root / "alias-out" / "file.txt", str(root)))

    def test_alternate_case_existing_and_new_paths_resolve_inside(self):
        root = "/tmp/RepoCase"
        with patch.object(
                et.os.path, "samefile", side_effect=self._case_insensitive_samefile):
            self.assertEqual(
                et._norm("/tmp/repocase/Existing.txt", root), "Existing.txt")
            self.assertEqual(et._norm("/tmp/repocase/new.txt", root), "new.txt")

    def test_real_case_insensitive_filesystem_alias(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "RepoCase"
            root.mkdir()
            (root / "Existing.txt").write_text("fixture")
            alternate = root.with_name(root.name.swapcase())
            try:
                same_directory = os.path.samefile(alternate, root)
            except OSError:
                same_directory = False
            if not same_directory:
                self.skipTest("fixture filesystem is case-sensitive")

            self.assertEqual(
                et._norm(alternate / "Existing.txt", str(root)), "Existing.txt")
            self.assertEqual(et._norm(alternate / "new.txt", str(root)), "new.txt")


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


class ShellCommentTests(unittest.TestCase):
    """An unquoted '#' ends the executable part of the line.

    Reported on PR #37. The hand-written scanner replaced shlex, which had
    handled comments for free, so this regressed against main: a trailing
    comment mentioning a redirect blocked the command it annotated.
    """

    def test_redirect_inside_a_comment_is_not_a_write(self):
        self.assertEqual(et._redirect_targets("echo hi # > out.txt"), [])

    def test_whole_line_comment_is_not_a_write(self):
        self.assertEqual(et._redirect_targets("# write it with > out.txt"), [])

    def test_hash_inside_a_word_is_not_a_comment(self):
        # bash reads `hi#not-comment` as one word, so the redirect is real.
        # Getting this wrong is the dangerous direction: a genuine write
        # would slip past the hook disguised as a comment.
        self.assertIn("out.txt", et._redirect_targets("echo hi#not-comment > out.txt"))

    def test_quoted_hash_is_not_a_comment(self):
        self.assertIn("out.txt", et._redirect_targets('echo "# heading" > out.txt'))

    def test_comment_ends_at_the_newline_and_later_lines_still_lex(self):
        # Multi-line commands are routine for agents; a comment on line one
        # must not blind the scanner to a real write on line two.
        command = "echo hi # harmless > decoy.txt\nrm -f x && echo bye > real.txt"
        targets = et._redirect_targets(command)
        self.assertIn("real.txt", targets)
        self.assertNotIn("decoy.txt", targets)


class ProcessSubstitutionTests(unittest.TestCase):
    """`>(cmd)` names a pipe, not a file.

    Reported on PR #37: the scanner read the '>' as a redirect and reported
    the command inside the parens as the file being written.
    """

    def test_process_substitution_is_not_a_write(self):
        self.assertEqual(et._redirect_targets("echo >(cat)"), [])

    def test_process_substitution_does_not_hide_a_real_redirect(self):
        self.assertIn("out.txt", et._redirect_targets("echo >(cat) > out.txt"))

    def test_redirect_nested_inside_process_substitution_still_counts(self):
        # The body is ordinary shell, and this one genuinely writes.
        self.assertIn("inside.txt", et._redirect_targets("echo >(cat > inside.txt)"))

    def test_input_process_substitution_is_not_a_write(self):
        self.assertEqual(et._redirect_targets("diff <(sort a) <(sort b)"), [])


class DescriptorTargetTests(unittest.TestCase):
    """`>&` writes a file only when its target is provably a filename.

    Reported on PR #37. A descriptor move and an unresolved expansion were
    both classified as paths, blocking commands that write nothing.
    """

    def test_descriptor_move_is_not_a_write(self):
        self.assertEqual(et._redirect_targets("cmd 3>&4-"), [])
        self.assertEqual(et._redirect_targets("cmd >&-"), [])

    def test_expanded_descriptor_is_uncertain_and_not_reported(self):
        self.assertEqual(et._redirect_targets("fd=2; echo hi >&$fd"), [])

    def test_expansion_after_a_plain_redirect_is_still_a_write(self):
        # Only `>&` is ambiguous. After a plain '>' an expansion is a file,
        # and dropping it would trade a false positive for a false negative.
        self.assertIn("$HOME/out.txt", et._redirect_targets("echo hi > $HOME/out.txt"))

    def test_literal_filename_after_the_dup_operator_is_still_a_write(self):
        self.assertIn("out.txt", et._redirect_targets("echo hi >&out.txt"))


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

    def _run(self, payload, branch, touches, governed=False, root="/repo"):
        # Most decision tests use a synthetic /repo. Production repo_root()
        # always returns an existing directory, so emulate its inode identity
        # for that fixture; real temporary-directory tests use samefile itself.
        samefile = (
            patch.object(
                et.os.path,
                "samefile",
                side_effect=lambda left, right: (
                    os.path.normpath(os.fspath(left))
                    == os.path.normpath(os.fspath(right))
                ),
            )
            if root == "/repo"
            else nullcontext()
        )
        with samefile, \
             patch.object(et.sys, "stdin", io.StringIO(json.dumps(payload))), \
             patch.object(et, "repo_root", return_value=root), \
             patch.object(et, "current_branch", return_value=branch), \
             patch.object(et, "governed_repo", return_value=governed), \
             patch.object(et, "touches_for", return_value=touches):
            return et.main()

    def test_every_file_tool_is_blocked_on_governed_main(self):
        payloads = {
            "Edit": {"file_path": "/repo/app.py"},
            "Write": {"file_path": "/repo/app.py"},
            "MultiEdit": {"file_path": "/repo/app.py", "edits": []},
            "NotebookEdit": {"notebook_path": "/repo/analysis.ipynb"},
        }
        for tool, tool_input in payloads.items():
            with self.subTest(tool=tool):
                stderr = io.StringIO()
                with patch.object(et.sys, "stderr", stderr):
                    rc = self._run(
                        {"tool_name": tool, "tool_input": tool_input, "cwd": "/repo"},
                        "main", None, governed=True,
                    )
                self.assertEqual(rc, et.EXIT_BLOCK)
                self.assertIn("Claim an issue", stderr.getvalue())
                self.assertIn("create_branch.py --worktree", stderr.getvalue())

    def test_main_in_ungoverned_repo_is_allowed(self):
        rc = self._run(
            {"tool_name": "Edit", "tool_input": {"file_path": "/repo/app.py"},
             "cwd": "/repo"},
            "main", None, governed=False,
        )
        self.assertEqual(rc, et.EXIT_ALLOW)

    def test_governance_detection_failure_on_main_fails_open(self):
        rc = self._run(
            {"tool_name": "Write", "tool_input": {"file_path": "/repo/app.py"},
             "cwd": "/repo"},
            "master", None, governed=None,
        )
        self.assertEqual(rc, et.EXIT_ALLOW)

    def test_outside_repo_path_on_governed_main_is_allowed(self):
        rc = self._run(
            {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/scratch.py"},
             "cwd": "/repo"},
            "main", None, governed=True,
        )
        self.assertEqual(rc, et.EXIT_ALLOW)

    def test_scratch_branch_in_governed_repo_is_allowed(self):
        rc = self._run(
            {"tool_name": "Edit", "tool_input": {"file_path": "/repo/app.py"},
             "cwd": "/repo"},
            "scratch/experiment", None, governed=True,
        )
        self.assertEqual(rc, et.EXIT_ALLOW)

    def test_detected_bash_writes_are_blocked_on_governed_main(self):
        for command in (
            "echo x > /repo/app.py",
            "echo x | tee /repo/app.py",
            "sed -i '' 's/x/y/' /repo/app.py",
        ):
            with self.subTest(command=command):
                rc = self._run(
                    {"tool_name": "Bash", "tool_input": {"command": command},
                     "cwd": "/repo"},
                    "main", None, governed=True,
                )
                self.assertEqual(rc, et.EXIT_BLOCK)

    def test_bash_write_on_ungoverned_main_is_allowed(self):
        rc = self._run(
            {"tool_name": "Bash", "tool_input": {"command": "echo x > /repo/app.py"},
             "cwd": "/repo"},
            "main", None, governed=False,
        )
        self.assertEqual(rc, et.EXIT_ALLOW)

    def test_real_path_aliases_are_correct_on_main_and_issue_branch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "repo"
            outside = Path(temp_dir) / "outside"
            root.mkdir()
            outside.mkdir()
            alias_in = Path(temp_dir) / "repo-alias"
            alias_in.symlink_to(root, target_is_directory=True)
            alias_out = root / "alias-out"
            alias_out.symlink_to(outside, target_is_directory=True)

            inside_via_alias = str(alias_in / "README.md")
            outside_via_alias = str(alias_out / "file.txt")
            dot_dot_name = str(root / "..evil")

            for target in (inside_via_alias, dot_dot_name):
                with self.subTest(branch="main", target=target):
                    rc = self._run(
                        {"tool_name": "Edit", "tool_input": {"file_path": target},
                         "cwd": str(root)},
                        "main", None, governed=True, root=str(root),
                    )
                    self.assertEqual(rc, et.EXIT_BLOCK)

            self.assertEqual(self._run(
                {"tool_name": "Edit", "tool_input": {"file_path": outside_via_alias},
                 "cwd": str(root)},
                "main", None, governed=True, root=str(root),
            ), et.EXIT_ALLOW)

            self.assertEqual(self._run(
                {"tool_name": "Edit", "tool_input": {"file_path": inside_via_alias},
                 "cwd": str(root)},
                "fix/issue-9-alias", ["README.md"], governed=True, root=str(root),
            ), et.EXIT_ALLOW)
            self.assertEqual(self._run(
                {"tool_name": "Edit", "tool_input": {"file_path": dot_dot_name},
                 "cwd": str(root)},
                "fix/issue-9-alias", ["README.md"], governed=True, root=str(root),
            ), et.EXIT_BLOCK)
            self.assertEqual(self._run(
                {"tool_name": "Edit", "tool_input": {"file_path": outside_via_alias},
                 "cwd": str(root)},
                "fix/issue-9-alias", ["README.md"], governed=True, root=str(root),
            ), et.EXIT_ALLOW)

    def test_alternate_case_paths_are_governed_on_main_and_issue_branch(self):
        root = "/tmp/RepoCase"

        def case_insensitive_samefile(left, right):
            return os.fspath(left).lower() == os.fspath(right).lower()

        with patch.object(et.os.path, "samefile", side_effect=case_insensitive_samefile):
            for target in ("/tmp/repocase/Existing.txt", "/tmp/repocase/new.txt"):
                with self.subTest(branch="main", target=target):
                    self.assertEqual(self._run(
                        {"tool_name": "Edit", "tool_input": {"file_path": target},
                         "cwd": root},
                        "main", None, governed=True, root=root,
                    ), et.EXIT_BLOCK)

                with self.subTest(branch="issue", target=target):
                    self.assertEqual(self._run(
                        {"tool_name": "Edit", "tool_input": {"file_path": target},
                         "cwd": root},
                        "fix/issue-9-case", [os.path.basename(target)],
                        governed=True, root=root,
                    ), et.EXIT_ALLOW)

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
