import io
import json
import os
import subprocess
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
        self.assertIsNotNone(et._git_write_to_protected("git push origin +main", "feat/issue-1-a"))
        self.assertIsNotNone(
            et._git_write_to_protected("git push origin +HEAD:refs/heads/main", "feat/issue-1-a")
        )
        self.assertIsNotNone(et._git_write_to_protected("git push origin HEAD:main", "feat/issue-1-a"))
        self.assertIsNotNone(
            et._git_write_to_protected("git push origin refs/heads/master", "feat/issue-1-a")
        )

    def test_quoted_ref_is_still_detected(self):
        self.assertIsNotNone(et._git_write_to_protected('git push origin "main"', "feat/issue-1-a"))

    def test_push_with_option_containing_quotes_detects_violation(self):
        self.assertIsNotNone(
            et._git_write_to_protected('git push --no-verify -o "message=it\'s" origin HEAD:main', "feat/issue-1-a")
        )

    def test_bare_push_while_on_main_is_blocked(self):
        self.assertIsNotNone(et._git_write_to_protected("git push", "main"))

    def test_omitted_or_configured_refspec_fails_closed(self):
        self.assertIsNotNone(et._git_write_to_protected("git push", "feat/issue-1-a"))
        self.assertIsNotNone(et._git_write_to_protected("git push origin", "feat/issue-1-a"))

    def test_explicit_feature_refspec_is_allowed(self):
        self.assertIsNone(et._git_write_to_protected("git push -u origin HEAD", "feat/issue-1-a"))

    def test_all_ref_push_forms_are_blocked_from_feature_branches(self):
        self.assertIsNotNone(et._git_write_to_protected("git push --all origin", "feat/issue-1-a"))
        self.assertIsNotNone(et._git_write_to_protected("git push --mirror origin", "feat/issue-1-a"))
        self.assertIsNotNone(
            et._git_write_to_protected(
                "git push origin refs/heads/*:refs/heads/*", "feat/issue-1-a"
            )
        )
        self.assertIsNotNone(
            et._git_write_to_protected("git push origin refs/heads/*", "feat/issue-1-a")
        )
        self.assertIsNotNone(et._git_write_to_protected("git push origin :", "feat/issue-1-a"))
        self.assertIsNotNone(et._git_write_to_protected("git push origin +:", "feat/issue-1-a"))

    def test_unrelated_command_is_allowed(self):
        self.assertIsNone(et._git_write_to_protected("pytest -q", "main"))

    def test_raw_prose_commands_are_safe_direct_to_matcher(self):
        self.assertIsNone(et._git_write_to_protected('echo "remember to git commit later"', "main"))
        self.assertIsNone(et._git_write_to_protected("echo 'run git commit when done'", "main"))
        self.assertIsNone(et._git_write_to_protected("cat << 'EOF'\nremember to git commit later\nEOF", "main"))
        self.assertIsNone(et._git_write_to_protected("ls # then git commit", "main"))
        self.assertIsNone(et._git_write_to_protected('git status -m "git push origin main"', "main"))
        self.assertIsNone(et._git_write_to_protected("echo git commit on main", "main"))
        self.assertIsNone(et._git_write_to_protected("printf %s git push origin main", "main"))

    def test_push_with_following_command_containing_main_is_allowed(self):
        self.assertIsNone(et._git_write_to_protected("git push origin feat && echo main", "feat/issue-1-a"))
        self.assertIsNone(et._git_write_to_protected("git push origin feat ; printf main", "feat/issue-1-a"))
        self.assertIsNone(et._git_write_to_protected("git push origin feat | grep main", "feat/issue-1-a"))

    def test_push_head_on_protected_branch_is_blocked(self):
        self.assertIsNotNone(et._git_write_to_protected("git push origin HEAD", "main"))
        self.assertIsNotNone(et._git_write_to_protected("git push origin HEAD:HEAD", "main"))
        self.assertIsNotNone(et._git_write_to_protected("git push origin @", "main"))
        self.assertIsNotNone(
            et._git_write_to_protected("git push origin +HEAD:heads/main", "feat/issue-1-a")
        )
        self.assertIsNotNone(
            et._git_write_to_protected("git push origin --delete heads/main", "feat/issue-1-a")
        )
        self.assertIsNotNone(
            et._git_write_to_protected("git push origin '@{upstream}'", "feat/issue-1-a")
        )

    def test_absolute_git_executable_path_is_blocked(self):
        self.assertIsNotNone(et._git_write_to_protected("/usr/bin/git commit -m 'x'", "main"))
        self.assertIsNotNone(et._git_write_to_protected("/usr/local/bin/git commit -m 'x'", "main"))
        self.assertIsNotNone(et._git_write_to_protected("/opt/homebrew/bin/git push origin main", "feat/issue-1-a"))

    def test_complex_wrapper_invocations_are_blocked(self):
        self.assertIsNotNone(et._git_write_to_protected("sudo --user root git commit -m 'x'", "main"))
        self.assertIsNotNone(et._git_write_to_protected("sudo -u root /usr/bin/git commit -m 'x'", "main"))
        self.assertIsNotNone(et._git_write_to_protected("env --unset FOO git commit -m 'x'", "main"))
        self.assertIsNotNone(et._git_write_to_protected("time -f fmt git commit -m 'x'", "main"))
        self.assertIsNotNone(et._git_write_to_protected('env -S "git commit -m x"', "main"))
        self.assertIsNotNone(et._git_write_to_protected('env -S "/usr/bin/git commit -m x"', "main"))
        self.assertIsNotNone(
            et._git_write_to_protected("exec -a ignored /usr/bin/git commit -m x", "main")
        )
        self.assertIsNotNone(
            et._git_write_to_protected(
                "exec -a ignored /usr/bin/git push origin HEAD", "main"
            )
        )
        self.assertIsNotNone(et._git_write_to_protected("nice git push origin main", "main"))
        self.assertIsNotNone(
            et._git_write_to_protected("/usr/bin/nice -n 5 git push origin main", "main")
        )
        self.assertIsNotNone(
            et._git_write_to_protected(
                "/usr/bin/env -P /usr/bin /usr/bin/git push origin main", "main"
            )
        )
        for command in (
            'env -S"git push origin main"',
            'env -iS "git push origin main"',
            "exec -ca ignored /usr/bin/git push origin main",
            "exec -la ignored /usr/bin/git push origin main",
            "env -C/repo/main git push origin HEAD",
            "sudo -D/repo/main git push origin HEAD",
            "sudo -nD /repo/main git push origin HEAD",
        ):
            with self.subTest(command=command):
                self.assertIsNotNone(et._git_write_to_protected(command, "feat/issue-1-a"))

    def test_tag_only_pushes_are_allowed(self):
        self.assertIsNone(et._git_write_to_protected("git push --tags origin", "main"))
        self.assertIsNone(et._git_write_to_protected("git push origin --tags", "main"))
        self.assertIsNone(et._git_write_to_protected("git push --repo=origin --tags", "main"))
        self.assertIsNotNone(et._git_write_to_protected("git push --tags origin main", "main"))
        self.assertIsNone(et._git_write_to_protected("git push origin tag main", "main"))
        self.assertIsNone(et._git_write_to_protected("git push origin tag master", "main"))

    def test_delete_mode_treats_tag_name_as_a_deletion_destination(self):
        for command in (
            "git push --delete origin tag main",
            "git push origin --delete tag main",
            "git push -d origin tag main",
            "git push -vd origin tag main",
            "git push -dv origin tag main",
            "git push --del origin tag main",
            "git push --no-delete --delete origin tag main",
        ):
            with self.subTest(command=command):
                self.assertIsNotNone(et._git_write_to_protected(command, "feat/issue-1-a"))

    def test_push_negations_are_ordered_and_not_sticky(self):
        self.assertIsNotNone(
            et._git_write_to_protected("git push --tags --no-tags origin", "feat/issue-1-a")
        )
        self.assertIsNotNone(
            et._git_write_to_protected("git push --tags --no-tag origin", "feat/issue-1-a")
        )
        self.assertIsNotNone(
            et._git_write_to_protected("git push --tags --no-ta origin", "feat/issue-1-a")
        )
        self.assertIsNone(
            et._git_write_to_protected("git push --no-tags --tags origin", "feat/issue-1-a")
        )
        self.assertIsNone(
            et._git_write_to_protected("git push --no-tags --ta origin", "feat/issue-1-a")
        )
        self.assertIsNone(
            et._git_write_to_protected(
                "git push --delete --no-delete origin tag main", "feat/issue-1-a"
            )
        )
        self.assertIsNone(
            et._git_write_to_protected(
                "git push --branches --no-b --tags origin", "feat/issue-1-a"
            )
        )
        self.assertIsNotNone(
            et._git_write_to_protected("git push --no-branches --b origin", "feat/issue-1-a")
        )

    def test_positional_remote_overrides_repo_option_and_fails_closed(self):
        self.assertIsNotNone(
            et._git_write_to_protected("git push --repo=origin feature", "feat/issue-1-a")
        )

    def test_malformed_env_split_string_does_not_crash(self):
        self.assertIsNone(et._git_write_to_protected("env -S \"git commit -m '\"", "main"))
        self.assertIsNotNone(et._git_write_to_protected('/usr/bin/env -i FOO=bar /usr/bin/git commit -m x', "main"))


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

    def test_prose_commands_via_main(self):
        """Pins the prose false-positive fix for git commands in shell prose through main()."""
        allowed_commands = [
            'echo "remember to git commit later"',
            "echo 'run git commit when done'",
            "cat << 'EOF'\nremember to git commit later\nEOF",
            "ls # then git commit",
            'git status -m "git push origin main"',
            "echo git commit on main",
            "printf %s git push origin main",
            "echo hello && echo main",
            "git push --tags origin",
            "git push origin --tags",
            "git push --repo=origin --tags",
            "git push origin tag main",
            "git push origin tag master",
            "git push --no-tags --tags origin",
            "git push --no-tags --ta origin",
            "git push --delete --no-delete origin tag main",
            "git push --branches --no-b --tags origin",
        ]
        for command in allowed_commands:
            with self.subTest(command=command, expected="ALLOW"):
                rc = self._run(
                    {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": "/repo"},
                    "main", None, governed=True,
                )
                self.assertEqual(rc, et.EXIT_ALLOW)

        blocked_commands = [
            "git commit -m x",
            "/usr/bin/git commit -m x",
            "git push origin main",
            "git push origin HEAD",
            "git push origin HEAD && echo main",
            "env FOO=1 git commit -m x",
            "sudo git commit -m x",
            "sudo --user root git commit -m x",
            "env --unset FOO git commit -m x",
            "time -f fmt git commit -m x",
            'env -S "git commit -m x"',
            "exec -a ignored /usr/bin/git commit -m x",
            "exec -a ignored /usr/bin/git push origin HEAD",
            "git push origin @",
            "git push origin +HEAD:heads/main",
            "git push origin --delete heads/main",
            "nice git push origin main",
            "/usr/bin/nice -n 5 git push origin main",
            "/usr/bin/env -P /usr/bin /usr/bin/git push origin main",
            'env -S"git push origin main"',
            'env -iS "git push origin main"',
            "exec -ca ignored /usr/bin/git push origin main",
            "exec -la ignored /usr/bin/git push origin main",
            "git push --repo=origin feature",
            "git push --delete origin tag main",
            "git push origin --delete tag main",
            "git push -d origin tag main",
            "git push -vd origin tag main",
            "git push -dv origin tag main",
            "git push --del origin tag main",
            "git push --no-delete --delete origin tag main",
            "git push --tags --no-tags origin",
            "git push --tags --no-tag origin",
            "git push --tags --no-ta origin",
            "git push --no-branches --b origin",
        ]
        for command in blocked_commands:
            with self.subTest(command=command, expected="BLOCK"):
                rc = self._run(
                    {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": "/repo"},
                    "main", None, governed=True,
                )
                self.assertEqual(rc, et.EXIT_BLOCK)

    def test_git_write_violation_retargeting_via_main(self):
        """Pins the _git_write_violation delegation in main().

        Reverting main() to _git_write_to_protected(command, branch) would
        evaluate only the caller shell's branch ('feat/121') and allow a
        write retargeted to main, failing this test.
        """
        branches = {
            "/repo/main": "main",
            "/repo/feat": "feat/121",
        }

        def branch_for(path):
            norm = os.path.normpath(str(path))
            return branches.get(norm, "feat/121")

        # Caller in worktree feat/121, but git -C targets main -> MUST BLOCK
        payload_block = {
            "tool_name": "Bash",
            "tool_input": {"command": "git -C /repo/main commit -m x"},
            "cwd": "/repo/feat",
        }
        with patch.object(et.sys, "stdin", io.StringIO(json.dumps(payload_block))), \
             patch.object(et, "repo_root", return_value="/repo/feat"), \
             patch.object(et, "current_branch", side_effect=branch_for), \
             patch.object(et, "governed_repo", return_value=True):
            rc = et.main()
            self.assertEqual(rc, et.EXIT_BLOCK)

        for command in (
            "env -C /repo/main git push origin HEAD",
            "env --chdir /repo/main git push origin HEAD",
            "sudo -D /repo/main git push origin HEAD",
            "env -C/repo/main git push origin HEAD",
            "sudo -D/repo/main git push origin HEAD",
            "sudo -nD /repo/main git push origin HEAD",
        ):
            with self.subTest(command=command), \
                 patch.object(et.sys, "stdin", io.StringIO(json.dumps({
                     "tool_name": "Bash",
                     "tool_input": {"command": command},
                     "cwd": "/repo/feat",
                 }))), \
                 patch.object(et, "repo_root", return_value="/repo/feat"), \
                 patch.object(et, "current_branch", side_effect=branch_for), \
                 patch.object(et, "governed_repo", return_value=True):
                self.assertEqual(et.main(), et.EXIT_BLOCK)

        # Caller on main, but git -C targets worktree feat/121 -> MUST ALLOW
        payload_allow = {
            "tool_name": "Bash",
            "tool_input": {"command": "git -C /repo/feat commit -m x"},
            "cwd": "/repo/main",
        }
        with patch.object(et.sys, "stdin", io.StringIO(json.dumps(payload_allow))), \
             patch.object(et, "repo_root", return_value="/repo/main"), \
             patch.object(et, "current_branch", side_effect=branch_for), \
             patch.object(et, "governed_repo", return_value=True):
            rc = et.main()
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


class WorktreeGovernanceTests(unittest.TestCase):
    """Governance follows the file, not the shell.

    Every agent in the fleet works inside `.worktrees/<branch>` while its
    shell may sit anywhere, so the repository that owns the target file and
    the repository the shell is standing in are routinely different. Deciding
    from the shell's cwd got this wrong in both directions: it refused writes
    inside a legitimate issue worktree, and - the dangerous half - it allowed
    writes into the `main` checkout and into other agents' worktrees, where
    neither the protected-branch guard nor `touches:` was ever consulted.

    These use real git worktrees rather than patched helpers. The defect is in
    how git state is resolved, so a fixture that stubs that resolution would
    assert nothing.
    """

    AGENTS_MD = "# AGENTS\n\n## Core Governance Directive: The Issue-First Law\n\nBody.\n"

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        base = Path(cls._tmp.name)
        cls.main_root = base / "repo"
        cls.main_root.mkdir()

        def git(*args, cwd):
            subprocess.run(
                ["git", *args], cwd=str(cwd), check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

        git("init", "-b", "main", cwd=cls.main_root)
        git("config", "user.email", "t@example.com", cwd=cls.main_root)
        git("config", "user.name", "t", cwd=cls.main_root)
        (cls.main_root / "AGENTS.md").write_text(cls.AGENTS_MD, encoding="utf-8")
        (cls.main_root / "app.py").write_text("x = 1\n", encoding="utf-8")
        git("add", "-A", cwd=cls.main_root)
        git("commit", "-m", "init", cwd=cls.main_root)

        # Two sibling worktrees, as the fleet actually runs: one per agent,
        # each on its own governed issue branch.
        cls.wt_a = cls.main_root / ".worktrees" / "fix-issue-11"
        cls.wt_b = cls.main_root / ".worktrees" / "fix-issue-22"
        git("worktree", "add", "-b", "fix/issue-11-a", str(cls.wt_a), cwd=cls.main_root)
        git("worktree", "add", "-b", "fix/issue-22-b", str(cls.wt_b), cwd=cls.main_root)

        # An unrelated repository that merely sits nearby. It must stay
        # ungoverned no matter which shell reaches for it.
        cls.outsider = base / "outsider"
        cls.outsider.mkdir()
        git("init", "-b", "main", cwd=cls.outsider)
        (cls.outsider / "app.py").write_text("y = 2\n", encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def decide(self, cwd, target, touches=("app.py",)):
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(target)},
            "cwd": str(cwd),
        }
        # Only the GitHub lookup is stubbed; all git resolution is real.
        with patch.object(et.sys, "stdin", io.StringIO(json.dumps(payload))), \
             patch.object(et, "touches_for", return_value=list(touches)):
            return et.main()

    # --- the false refusal ------------------------------------------------

    def test_write_inside_own_worktree_is_allowed_from_the_worktree(self):
        self.assertEqual(self.decide(self.wt_a, self.wt_a / "app.py"), et.EXIT_ALLOW)

    def test_write_inside_own_worktree_is_allowed_from_the_repo_root(self):
        """The shell sitting on main must not make a worktree edit a violation.

        Working in a worktree while the shell stays at the repository root is
        an ordinary pattern; refusing it teaches agents that the hook is noise.
        """
        self.assertEqual(
            self.decide(self.main_root, self.wt_a / "app.py"), et.EXIT_ALLOW
        )

    # --- the false permissions -------------------------------------------

    def test_write_into_main_checkout_is_blocked_from_a_worktree(self):
        """The dangerous direction: this is the gap #29 closed, reopened.

        An agent standing in any issue worktree could write straight into the
        checkout that has `main` out, and the protected-branch guard never
        fired because it was asked about the worktree's branch instead.
        """
        self.assertEqual(
            self.decide(self.wt_a, self.main_root / "app.py"), et.EXIT_BLOCK
        )

    def test_write_into_main_checkout_is_blocked_from_the_repo_root(self):
        self.assertEqual(
            self.decide(self.main_root, self.main_root / "app.py"), et.EXIT_BLOCK
        )

    def test_write_into_another_agents_worktree_is_governed_by_that_worktree(self):
        """Cross-agent isolation is the whole point of `touches:`.

        #22's worktree is judged by #22's declaration, not #11's. `other.py`
        is outside the declaration, so this is a violation rather than an
        unchecked write.
        """
        self.assertEqual(
            self.decide(self.wt_a, self.wt_b / "other.py"), et.EXIT_BLOCK
        )

    def test_declared_path_in_another_worktree_is_still_allowed(self):
        """Governed by the other worktree's issue - not blocked reflexively."""
        self.assertEqual(
            self.decide(self.wt_a, self.wt_b / "app.py"), et.EXIT_ALLOW
        )

    # --- containment and fail-open ---------------------------------------

    def test_path_outside_the_declaration_in_own_worktree_is_blocked(self):
        self.assertEqual(
            self.decide(self.wt_a, self.wt_a / "elsewhere.py"), et.EXIT_BLOCK
        )

    def test_unrelated_repository_is_never_governed(self):
        """A sibling checkout that is not this repository stays the user's own.

        The hook is installed globally, so mistaking a neighbouring project
        for a governed worktree would make that project unusable.
        """
        self.assertEqual(
            self.decide(self.wt_a, self.outsider / "app.py"), et.EXIT_ALLOW
        )
        self.assertEqual(
            self.decide(self.main_root, self.outsider / "app.py"), et.EXIT_ALLOW
        )

    def test_path_in_no_repository_at_all_is_allowed(self):
        self.assertEqual(
            self.decide(self.wt_a, Path(self._tmp.name) / "loose.txt"), et.EXIT_ALLOW
        )

    def bash(self, cwd, command, touches=("app.py",)):
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": str(cwd),
        }
        with patch.object(et.sys, "stdin", io.StringIO(json.dumps(payload))), \
             patch.object(et, "touches_for", return_value=list(touches)):
            return et.main()

    def test_redirect_into_main_checkout_is_blocked_from_a_worktree(self):
        """A shell redirect must not be the way around the write path.

        Fixing only Edit/Write would leave `echo x > <main>/app.py` as an
        unguarded equivalent, which is how this class of gap keeps reappearing.
        """
        self.assertEqual(
            self.bash(self.wt_a, f"echo x > {self.main_root / 'app.py'}"),
            et.EXIT_BLOCK,
        )

    def test_redirect_inside_own_worktree_is_still_allowed(self):
        self.assertEqual(
            self.bash(self.wt_a, f"echo x > {self.wt_a / 'app.py'}"), et.EXIT_ALLOW
        )

    def test_redirect_into_another_worktree_outside_its_touches_is_blocked(self):
        """Raised in review of PR #70.

        The protected-branch half of the Bash path used the owning checkout
        while the touches half still keyed off the session root, so a redirect
        at an undeclared path in a sibling worktree was allowed even though
        the identical Write was refused.
        """
        self.assertEqual(
            self.bash(self.wt_a, f"echo x > {self.wt_b / 'other.py'}"), et.EXIT_BLOCK
        )

    def test_redirect_into_another_worktree_within_its_touches_is_allowed(self):
        """Governed by the owner's declaration, not blocked reflexively."""
        self.assertEqual(
            self.bash(self.wt_a, f"echo x > {self.wt_b / 'app.py'}"), et.EXIT_ALLOW
        )

    def test_bash_and_write_agree_on_every_cross_checkout_target(self):
        """Parity is the property that keeps a redirect from being a bypass.

        Asserted as a loop rather than as separate cases so a future target
        cannot be fixed on one path and forgotten on the other.
        """
        for target in (
            self.wt_a / "app.py",
            self.wt_a / "elsewhere.py",
            self.wt_b / "app.py",
            self.wt_b / "other.py",
            self.main_root / "app.py",
            self.outsider / "app.py",
        ):
            for cwd in (self.wt_a, self.main_root):
                with self.subTest(target=str(target), cwd=str(cwd)):
                    self.assertEqual(
                        self.bash(cwd, f"echo x > {target}"),
                        self.decide(cwd, target),
                    )

    def test_new_file_in_a_directory_that_does_not_exist_yet_is_governed(self):
        """Write creates parents, so the target's directory need not exist.

        Resolution has to walk up to the nearest existing ancestor; otherwise
        every new file in a new package silently escapes governance.
        """
        self.assertEqual(
            self.decide(self.wt_a, self.main_root / "brand" / "new" / "f.py"),
            et.EXIT_BLOCK,
        )


class CacheRecheckTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        et._run(["git", "init", "-b", "main"], cwd=self.root)
        # Identity required by current_branch/worktree operations on some CI images.
        et._run(["git", "config", "user.email", "t@example.com"], cwd=self.root)
        et._run(["git", "config", "user.name", "t"], cwd=self.root)
        (self.root / "AGENTS.md").write_text("# Core Governance: The Issue-First Law\n", encoding="utf-8")
        et._run(["git", "add", "."], cwd=self.root)
        et._run(["git", "commit", "-m", "init"], cwd=self.root)

        # Create worktree. Parent dir must exist on older git; do not rely on
        # `worktree add` creating intermediate directories.
        self.issue = 73
        self.wt = self.root / ".worktrees" / "feat-issue-73-test"
        self.wt.parent.mkdir(parents=True, exist_ok=True)
        rc, out = et._run(
            ["git", "worktree", "add", "-b", "feat/issue-73-test", str(self.wt)],
            cwd=self.root,
        )
        if rc != 0:
            self.fail(f"git worktree add failed: {out!r}")
        branch = et.current_branch(str(self.wt))
        if et.issue_from_branch(branch) != self.issue:
            self.fail(f"worktree branch {branch!r} does not carry issue #{self.issue}")

    def tearDown(self):
        self._tmp.cleanup()

    def test_cache_resolves_to_common_git_dir_from_worktree(self):
        cache_path = et._cache_path(self.wt, self.issue)
        self.assertIsNotNone(cache_path)
        self.assertTrue(cache_path.endswith(f"aru-touches-{self.issue}.json"))
        # Must resolve to the main .git directory, not .worktrees/.../.git
        self.assertIn(os.path.realpath(str(self.root / ".git")), os.path.realpath(cache_path))

    def test_narrow_cache_rechecks_github_on_unallowed_path_and_allows_when_widened(self):
        et._write_cache(self.wt, self.issue, ["app.py"])
        self.assertEqual(et._read_cache(self.wt, self.issue), ["app.py"])
        self.assertFalse(et.path_allowed("extra.py", ["app.py"]))

        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(self.wt / "extra.py")},
            "cwd": str(self.wt),
        }
        real_run = et._run
        gh_calls = []
        decisions = []

        def mock_run(cmd, cwd=None, timeout=15):
            # Match _run's signature so a timeout kwarg cannot bypass the mock
            # and hit a real `gh` (which fail-opens and leaves the cache stale).
            if cmd and cmd[0] == "gh":
                gh_calls.append(cmd)
                return 0, "touches: app.py, extra.py"
            return real_run(cmd, cwd=cwd, timeout=timeout)

        real_touches_for = et.touches_for

        def tracing_touches_for(root, issue, force_refresh=False):
            result = real_touches_for(root, issue, force_refresh=force_refresh)
            decisions.append((str(root), issue, force_refresh, list(result) if result is not None else None))
            return result

        with patch.object(et.sys, "stdin", io.StringIO(json.dumps(payload))), \
             patch.object(et, "_run", side_effect=mock_run), \
             patch.object(et, "touches_for", side_effect=tracing_touches_for):
            res = et.main()
        self.assertEqual(
            res, et.EXIT_ALLOW,
            f"decisions={decisions!r} gh_calls={gh_calls!r} "
            f"branch={et.current_branch(str(self.wt))!r}",
        )
        self.assertTrue(
            any(force for _root, _issue, force, _touches in decisions),
            f"expected a force-refresh touches lookup; decisions={decisions!r}",
        )
        self.assertTrue(gh_calls, "expected a force-refresh gh issue view")
        # Verify cache was updated
        self.assertEqual(et._read_cache(self.wt, self.issue), ["app.py", "extra.py"])

    def test_allowed_path_hits_cache_and_makes_no_github_call(self):
        et._write_cache(self.wt, self.issue, ["app.py"])

        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(self.wt / "app.py")},
            "cwd": str(self.wt),
        }
        real_run = et._run
        def mock_run(cmd, cwd=None, timeout=15):
            if cmd and cmd[0] == "gh":
                raise AssertionError("GitHub CLI should not be called on cached allow")
            return real_run(cmd, cwd=cwd, timeout=timeout)

        with patch.object(et.sys, "stdin", io.StringIO(json.dumps(payload))), \
             patch.object(et, "_run", side_effect=mock_run):
            res = et.main()
        self.assertEqual(res, et.EXIT_ALLOW)

    def test_github_failure_on_recheck_fails_open(self):
        et._write_cache(self.wt, self.issue, ["app.py"])

        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(self.wt / "unregistered.py")},
            "cwd": str(self.wt),
        }
        real_run = et._run
        def mock_run(cmd, cwd=None, timeout=15):
            if cmd and cmd[0] == "gh":
                return 1, "API rate limit exceeded"
            return real_run(cmd, cwd=cwd, timeout=timeout)

        with patch.object(et.sys, "stdin", io.StringIO(json.dumps(payload))), \
             patch.object(et, "_run", side_effect=mock_run):
            res = et.main()
        self.assertEqual(res, et.EXIT_ALLOW)


class LeadingExpansionTests(unittest.TestCase):
    """A redirect target beginning with an expansion is not a repo path (#76).

    `echo x > $TMP/out.txt` was reported as a write to `<repo>/$TMP/out.txt`
    and refused against `touches:`, even when `$TMP` points elsewhere. The
    refusal named `touches:`, so the readings available to an agent were
    "widen the declaration" or "this path is forbidden" - and widening a
    declaration to include `$TMP` is nonsense.

    Only the *leading* segment decides the root, so an expansion later in the
    path stays repository-relative and governed.
    """

    def test_unresolvable_leading_expansion_has_no_knowable_destination(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(et._resolve_target("$TMP/out.txt"))

    def test_the_exact_command_that_failed_is_resolvable_to_nothing(self):
        # From the #76 report: TMP is assigned inside the command, so the hook
        # cannot see it. Regression test for the original failure.
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(et._resolve_target("$TMP/x"))

    def test_leading_expansion_that_resolves_is_judged_by_its_real_destination(self):
        with patch.dict(os.environ, {"HOME": "/Users/someone"}, clear=True):
            self.assertEqual(et._resolve_target("$HOME/out.txt"), "/Users/someone/out.txt")

    def test_braced_expansion_resolves_too(self):
        with patch.dict(os.environ, {"HOME": "/Users/someone"}, clear=True):
            self.assertEqual(et._resolve_target("${HOME}/out.txt"), "/Users/someone/out.txt")

    def test_resolvable_expansion_keeps_its_coverage(self):
        # Coverage must not be silently lost: a variable naming a real in-repo
        # path still resolves and stays governed.
        with patch.dict(os.environ, {"ARU_SDLC_HOME": "/repo"}, clear=True):
            self.assertEqual(et._resolve_target("$ARU_SDLC_HOME/AGENTS.md"), "/repo/AGENTS.md")

    def test_expansion_after_the_first_segment_stays_repository_relative(self):
        # `dir/` roots this in the repository regardless of what $name holds.
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(et._resolve_target("dir/$name.txt"), "dir/$name.txt")

    def test_ordinary_relative_target_is_unchanged(self):
        self.assertEqual(et._resolve_target("out.txt"), "out.txt")

    def test_absolute_target_is_unchanged(self):
        self.assertEqual(et._resolve_target("/tmp/out.txt"), "/tmp/out.txt")

    def test_command_substitution_in_the_leading_segment_is_unknowable(self):
        self.assertIsNone(et._resolve_target("$(mktemp -d)/out.txt"))

    def test_bare_variable_with_no_separator_is_unknowable(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(et._resolve_target("$OUTFILE"))


class GitCommandCheckoutTests(unittest.TestCase):
    """A git write is judged by the checkout it targets, not the shell (#117).

    #70 moved *path* governance onto the owning checkout, but a git
    subcommand has no path operand, so `_git_write_to_protected` kept reading
    the session's branch. Two failures followed, and the dangerous one is the
    permission: from any issue worktree, `git -C <main> commit` and
    `cd <main> && git commit` reached `main` with the guard never consulted.

    Compounding it, the old pattern allowed only lowercase `-c <config>`
    between `git` and the subcommand, so `-C <path>`, `--git-dir=` and
    `--work-tree=` stopped the command being recognised as a commit at all.

    Real worktrees, not patched helpers: the defect is in how git state is
    resolved, so stubbing that resolution would assert nothing.
    """

    AGENTS_MD = "# AGENTS\n\n## Core Governance Directive: The Issue-First Law\n\nBody.\n"

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        base = Path(cls._tmp.name)
        cls.main_root = base / "repo"
        cls.main_root.mkdir()

        def git(*args, cwd):
            subprocess.run(
                ["git", *args], cwd=str(cwd), check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

        git("init", "-b", "main", cwd=cls.main_root)
        git("config", "user.email", "t@example.com", cwd=cls.main_root)
        git("config", "user.name", "t", cwd=cls.main_root)
        (cls.main_root / "AGENTS.md").write_text(cls.AGENTS_MD, encoding="utf-8")
        (cls.main_root / "app.py").write_text("x = 1\n", encoding="utf-8")
        git("add", "-A", cwd=cls.main_root)
        git("commit", "-m", "init", cwd=cls.main_root)

        cls.wt = cls.main_root / ".worktrees" / "fix-issue-11"
        git("worktree", "add", "-b", "fix/issue-11-a", str(cls.wt), cwd=cls.main_root)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def violation(self, command, cwd):
        return et._git_write_violation(command, str(cwd))

    # --- unchanged behaviour ---------------------------------------------

    def test_plain_commit_on_main_is_still_blocked(self):
        self.assertIsNotNone(self.violation("git commit -m x", self.main_root))

    def test_plain_commit_in_a_worktree_is_still_allowed(self):
        self.assertIsNone(self.violation("git commit -m x", self.wt))

    def test_unrelated_command_is_still_allowed(self):
        self.assertIsNone(self.violation("pytest -q", self.main_root))

    def test_explicit_push_to_main_from_a_worktree_is_still_blocked(self):
        self.assertIsNotNone(self.violation("git push origin main", self.wt))

    # --- the false permissions (shell in a worktree, target is main) ------

    def test_dash_capital_c_commit_into_main_is_blocked(self):
        self.assertIsNotNone(
            self.violation(f"git -C {self.main_root} commit -m x", self.wt)
        )

    def test_dash_capital_c_push_into_main_is_blocked(self):
        self.assertIsNotNone(
            self.violation(f"git -C {self.main_root} push origin main", self.wt)
        )

    def test_git_dir_and_work_tree_commit_into_main_is_blocked(self):
        self.assertIsNotNone(
            self.violation(
                f"git --git-dir={self.main_root}/.git "
                f"--work-tree={self.main_root} commit -m x",
                self.wt,
            )
        )

    def test_config_flag_before_capital_c_still_resolves(self):
        self.assertIsNotNone(
            self.violation(
                f"git -C {self.main_root} -c user.name=x commit -m x", self.wt
            )
        )

    def test_cd_into_main_then_commit_is_blocked(self):
        self.assertIsNotNone(
            self.violation(f"cd {self.main_root} && git commit -m x", self.wt)
        )

    def test_cd_into_main_then_bare_push_is_blocked(self):
        self.assertIsNotNone(
            self.violation(f"cd {self.main_root}; git push", self.wt)
        )

    def test_subshell_cd_into_main_then_commit_is_blocked(self):
        self.assertIsNotNone(
            self.violation(f"(cd {self.main_root} && git commit -m x)", self.wt)
        )

    # --- the false refusal (shell on main, target is a worktree) ----------

    def test_cd_into_worktree_then_commit_is_allowed(self):
        """How this was found: committing under claim from a shell on main."""
        self.assertIsNone(
            self.violation(f"cd {self.wt} && git commit -m x", self.main_root)
        )

    def test_dash_capital_c_commit_in_a_worktree_is_allowed(self):
        self.assertIsNone(
            self.violation(f"git -C {self.wt} commit -m x", self.main_root)
        )

    # --- ordering: a cd after the git command must not retarget it --------

    def test_cd_after_the_commit_does_not_excuse_it(self):
        self.assertIsNotNone(
            self.violation(f"git commit -m x; cd {self.wt}", self.main_root)
        )

    # --- fail closed on what cannot be resolved ---------------------------

    def test_unresolvable_cd_target_before_a_commit_is_refused(self):
        """Cannot prove it is safe, so refuse.

        Enumerating shell constructs reproduces this defect in a new place;
        the invariant is that an unprovable git write is refused. A refusal
        costs one explicit command, a false permission costs an ungoverned
        commit on `main`.
        """
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNotNone(
                self.violation("cd $SOMEWHERE && git commit -m x", self.wt)
            )

    def test_unresolvable_dash_capital_c_target_is_refused(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNotNone(
                self.violation("git -C $SOMEWHERE commit -m x", self.wt)
            )

    def test_resolvable_cd_target_is_not_refused_for_being_a_variable(self):
        with patch.dict(os.environ, {"WT": str(self.wt)}, clear=False):
            self.assertIsNone(
                self.violation("cd $WT && git commit -m x", self.main_root)
            )

    def test_unresolvable_target_without_a_git_write_is_ignored(self):
        # Fail-closed applies to git writes only; ordinary commands are not
        # this function's business.
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(self.violation("cd $SOMEWHERE && ls", self.wt))


if __name__ == "__main__":
    unittest.main()
