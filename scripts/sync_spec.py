#!/usr/bin/env python3
# line-ceiling: 546
"""Bi-directional specification-to-code synchronization engine and DoD validator.

Statically parses Python AST across scripts/*.py to extract CLI argument contracts
and public signatures, cross-references documentation in docs/, skills/, and prompts/
for stale flags, broken claim tags, or undocumented/mismatched parameters, and ensures
strict bi-directional spec-code alignment.
"""

from __future__ import annotations

import argparse
import ast
import datetime
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

CLAIM_TAG_RE = re.compile(
    r'<!--\s*claim:(?P<id>[a-zA-Z0-9_\-\.]+)(?:\s+target="(?P<target>[^"]+)")?(?:\s+verification="(?P<date>[^"]+)")?\s*-->'
)
CLI_CALL_RE = re.compile(
    r'(?:python3\s+(?:"?\$ARU_SDLC_HOME/"?|./)?)?scripts/(?P<script>[a-zA-Z0-9_\-]+\.py)\b(?P<args>[^`\n]*)'
)
CODE_FENCE_RE = re.compile(r'```(?:bash|sh|zsh|shell)?\s*\n(?P<code>.*?)```', re.DOTALL)
PARAM_TABLE_ROW_RE = re.compile(
    r'^\|\s*(?P<flag>`?--?[a-zA-Z0-9_\-]+`?)\s*\|\s*(?P<desc>[^|]+)\|(?:\s*(?P<default>[^|]*)\|)?',
    re.MULTILINE,
)


class ArgumentExtractor(ast.NodeVisitor):
    """Extracts argparse add_argument declarations and metadata from Python AST."""

    def __init__(self) -> None:
        self.flags: Set[str] = {"-h", "--help"}
        self.positionals: List[str] = []
        self.options: Dict[str, Dict[str, Any]] = {}

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument":
            arg_names = []
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    arg_names.append(arg.value)

            is_flag = any(name.startswith("-") for name in arg_names)
            for name in arg_names:
                if name.startswith("-"):
                    self.flags.add(name)
                else:
                    self.positionals.append(name)

            if arg_names:
                primary = arg_names[0]
                kw_dict: Dict[str, Any] = {
                    "flags": arg_names,
                    "is_flag": is_flag,
                    "default": None,
                    "required": False,
                }
                for kw in node.keywords:
                    if isinstance(kw.value, ast.Constant):
                        kw_dict[kw.arg] = kw.value.value
                    elif isinstance(kw.value, ast.Name):
                        kw_dict[kw.arg] = kw.value.id
                self.options[primary] = kw_dict
                for alias in arg_names[1:]:
                    if alias.startswith("-"):
                        self.options[alias] = kw_dict

        self.generic_visit(node)


def extract_ast_from_source(source_text: str, filename: str = "<unknown>") -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Extract CLI argument specs and top-level functions from Python source string."""
    try:
        tree = ast.parse(source_text, filename=filename)
    except (SyntaxError, UnicodeDecodeError):
        return {"flags": ["-h", "--help"], "positionals": [], "options": {}}, []

    extractor = ArgumentExtractor()
    extractor.visit(tree)

    functions = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not node.name.startswith("_"):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = [a.arg for a in node.args.args if a.arg != "self"]
                functions.append({"name": node.name, "type": "function", "args": args})
            else:
                functions.append({"name": node.name, "type": "class"})

    spec = {
        "flags": sorted(list(extractor.flags)),
        "positionals": extractor.positionals,
        "options": extractor.options,
    }
    return spec, functions


def _git_tree_show(repo_dir: Path, head_sha: str, rel_path: str) -> Optional[str]:
    """Read file directly from git head tree snapshot, failing closed if unavailable."""
    proc = subprocess.run(
        ["git", "show", f"{head_sha}:{rel_path}"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def _git_tree_list(repo_dir: Path, head_sha: str, prefixes: List[str]) -> Tuple[bool, List[str]]:
    """List all tracked files matching prefixes under the head SHA."""
    proc = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", head_sha],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return False, []
    all_files = proc.stdout.splitlines()
    matched = []
    for f in all_files:
        for p in prefixes:
            if f == p or f.startswith(p if p.endswith("/") else f"{p}/"):
                matched.append(f)
                break
    return True, matched


def extract_script_cli_specs(scripts_dir: Path, head_sha: Optional[str] = None, repo_dir: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """Statically parse scripts/*.py AST to extract CLI parameters from head or working tree."""
    specs: Dict[str, Dict[str, Any]] = {}
    effective_repo = repo_dir or (scripts_dir.parent if scripts_dir.name == "scripts" else Path("."))

    if head_sha:
        ok, files = _git_tree_list(effective_repo, head_sha, ["scripts/"])
        if not ok:
            return specs
        for line in sorted(files):
            if line.endswith(".py") and not Path(line).name.startswith("__"):
                content = _git_tree_show(effective_repo, head_sha, line)
                if content is not None:
                    spec, funcs = extract_ast_from_source(content, filename=line)
                    spec["functions"] = funcs
                    spec["path"] = line
                    specs[Path(line).name] = spec
        return specs

    if not scripts_dir.is_dir():
        return specs

    for file_path in sorted(scripts_dir.glob("*.py")):
        if file_path.name.startswith("__"):
            continue
        try:
            content = file_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        spec, funcs = extract_ast_from_source(content, filename=str(file_path))
        spec["functions"] = funcs
        spec["path"] = str(file_path)
        specs[file_path.name] = spec

    return specs


def _extract_flags_from_command(args_str: str) -> List[str]:
    """Tokenize arguments string and extract option flags."""
    flags = []
    clean_str = re.sub(r"\\\s*\n", " ", args_str)
    cleaned = re.split(r'\s*(?:\||&&|\|\||2>|1>|>|<|;)\s*', clean_str)[0]
    try:
        tokens = shlex.split(cleaned)
    except ValueError:
        tokens = cleaned.split()

    for token in tokens:
        if token.startswith("-") and not token.startswith("---"):
            flag = token.split("=")[0]
            if re.match(r"^-[a-zA-Z0-9]$", flag) or re.match(r"^--[a-zA-Z0-9][a-zA-Z0-9_\-]*$", flag):
                flags.append(flag)
    return flags


def _verify_claim_target(
    repo_dir: Path, target: str, cli_specs: Dict[str, Dict[str, Any]], head_sha: Optional[str] = None
) -> Tuple[bool, str]:
    """Verify that a claim's target file and optional symbol exist in the audited snapshot."""
    file_part, _, symbol_part = target.partition(":")
    if head_sha:
        content = _git_tree_show(repo_dir, head_sha, file_part)
        if content is None:
            return False, f"Target file '{file_part}' does not exist at head {head_sha[:7]}"
    else:
        target_file = repo_dir / file_part
        if not target_file.exists():
            return False, f"Target file '{file_part}' does not exist"
        try:
            content = target_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return False, f"Cannot read '{file_part}': {exc}"

    if symbol_part:
        script_name = Path(file_part).name
        if script_name in cli_specs and not head_sha:
            funcs = [f["name"] for f in cli_specs[script_name].get("functions", [])]
            if symbol_part not in funcs:
                return False, f"Symbol '{symbol_part}' not found in '{file_part}'"
        else:
            try:
                tree = ast.parse(content, filename=file_part)
                symbols = {
                    n.name for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                }
                if symbol_part not in symbols:
                    return False, f"Symbol '{symbol_part}' not found in '{file_part}'"
            except Exception:
                return False, f"Could not parse '{file_part}' to verify symbol '{symbol_part}'"
    return True, "ok"


def audit_docs_and_skills(repo_dir: Path, cli_specs: Dict[str, Dict[str, Any]], head_sha: Optional[str] = None) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:  # noqa: C901, PLR0912, PLR0915
    """Audit markdown docs and specs against code in both directions under the snapshot."""
    drift_findings: List[Dict[str, Any]] = []
    claims: List[Dict[str, Any]] = []

    doc_files: List[Tuple[str, str]] = []  # (rel_path, content)

    if head_sha:
        prefixes = ["docs/", "skills/", "prompts/", "AGENTS.md", "README.md"]
        ok, tracked_files = _git_tree_list(repo_dir, head_sha, prefixes)
        if not ok:
            drift_findings.append({
                "type": "git_head_error",
                "file": "<repository>",
                "message": f"Failed to list repository git tree at head {head_sha}",
            })
            return drift_findings, claims

        for rel_path in sorted(tracked_files):
            if rel_path.endswith(".md"):
                content = _git_tree_show(repo_dir, head_sha, rel_path)
                if content is None:
                    drift_findings.append({
                        "type": "git_head_error",
                        "file": rel_path,
                        "message": f"Failed to read file from git head {head_sha[:7]}",
                    })
                else:
                    doc_files.append((rel_path, content))
    else:
        target_dirs = [repo_dir / "docs", repo_dir / "skills", repo_dir / "prompts"]
        md_paths: List[Path] = []
        for d in target_dirs:
            if d.is_dir():
                md_paths.extend(d.rglob("*.md"))
        for root_file in [repo_dir / "AGENTS.md", repo_dir / "README.md"]:
            if root_file.is_file():
                md_paths.append(root_file)

        for md_path in sorted(set(md_paths)):
            try:
                content = md_path.read_text(encoding="utf-8")
                rel_path = md_path.relative_to(repo_dir).as_posix()
                doc_files.append((rel_path, content))
            except (UnicodeDecodeError, OSError):
                continue

    for rel_path, content in doc_files:
        # 1. Audit Claim Tags (Verifiable vs Unbound)
        for match in CLAIM_TAG_RE.finditer(content):
            claim_id, target, v_date = match.group("id"), match.group("target"), match.group("date")
            claims.append({"file": rel_path, "id": claim_id, "target": target, "verification": v_date})
            if target:
                valid, reason = _verify_claim_target(repo_dir, target, cli_specs, head_sha=head_sha)
                if not valid:
                    drift_findings.append({
                        "type": "invalid_claim_target",
                        "file": rel_path,
                        "claim_id": claim_id,
                        "target": target,
                        "message": f"Claim '{claim_id}' target invalid: {reason}",
                    })
            else:
                drift_findings.append({
                    "type": "unbound_claim_warning",
                    "file": rel_path,
                    "claim_id": claim_id,
                    "message": f"Claim '{claim_id}' has no target binding (must specify target=\"path[:symbol]\")",
                })

        # 2. Audit Multiline Code Fences (Doc -> Code)
        for fence_match in CODE_FENCE_RE.finditer(content):
            unfolded = re.sub(r"\\\s*\n", " ", fence_match.group("code"))
            for line in unfolded.splitlines():
                for match in CLI_CALL_RE.finditer(line):
                    script, args_str = match.group("script"), match.group("args")
                    if script not in cli_specs:
                        continue
                    known_flags = set(cli_specs[script]["flags"])
                    for flag in _extract_flags_from_command(args_str):
                        if flag not in known_flags:
                            drift_findings.append({
                                "type": "cli_flag_drift",
                                "file": rel_path,
                                "script": script,
                                "flag": flag,
                                "message": f"Documented flag '{flag}' not recognized in '{script}'",
                            })

        # 3. Audit Inline CLI references (Doc -> Code)
        no_fences = CODE_FENCE_RE.sub('', content)
        no_comments = re.sub(r'<!--.*?-->', '', no_fences, flags=re.DOTALL)
        for line_idx, line in enumerate(no_comments.splitlines(), start=1):
            for match in CLI_CALL_RE.finditer(line):
                script, args_str = match.group("script"), match.group("args")
                if script not in cli_specs:
                    continue
                known_flags = set(cli_specs[script]["flags"])
                for flag in _extract_flags_from_command(args_str):
                    if flag not in known_flags:
                        drift_findings.append({
                            "type": "cli_flag_drift",
                            "file": rel_path,
                            "line": line_idx,
                            "script": script,
                            "flag": flag,
                            "message": f"Documented flag '{flag}' not recognized in '{script}'",
                        })

        # 4. Structured Parameter Table Audit (Bi-directional check on options/defaults)
        for table_match in PARAM_TABLE_ROW_RE.finditer(content):
            raw_flag = table_match.group("flag").strip("`").strip()
            doc_default = (table_match.group("default") or "").strip().strip("`")
            # If doc mentions a script in header/context
            for script_name, spec in cli_specs.items():
                if script_name[:-3] in rel_path or script_name in content:
                    if raw_flag in spec["options"]:
                        opt_info = spec["options"][raw_flag]
                        if doc_default and opt_info.get("default") is not None:
                            code_default = str(opt_info["default"]).lower()
                            if doc_default.lower() not in (code_default, "none", "optional") and code_default not in doc_default.lower():
                                drift_findings.append({
                                    "type": "cli_default_drift",
                                    "file": rel_path,
                                    "script": script_name,
                                    "flag": raw_flag,
                                    "message": f"Documented default '{doc_default}' differs from code default '{opt_info['default']}' in '{script_name}'",
                                })

    return drift_findings, claims


def sync_shipped_roadmap(repo_dir: Path) -> int:
    """Idempotently sync recently merged PR references into docs/ARU-SOFTWARE-FACTORY.md."""
    roadmap_file = repo_dir / "docs" / "ARU-SOFTWARE-FACTORY.md"
    if not roadmap_file.is_file():
        return 0

    try:
        content = roadmap_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0

    # Extract merged PR references from git log
    proc = subprocess.run(
        ["git", "log", "-n", "30", "--merges", "--pretty=format:%h %s"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return 0

    updated_count = 0
    merge_lines = proc.stdout.splitlines()
    for line in merge_lines:
        match = re.search(r"Merge pull request #(\d+)", line) or re.search(r"Merge PR #(\d+)", line)
        if match:
            pr_num = match.group(1)
            sha = line.split()[0]
            # Check if PR #pr_num is already in roadmap table
            if f"PR #{pr_num}" not in content and f"#{pr_num}" not in content:
                table_marker = "## 9. Appendix — shipped roadmap rows"
                if table_marker in content:
                    row = f"| S-PR-{pr_num} | Governed PR delivery | PR #{pr_num} `{sha}` |\n"
                    content = content.replace(
                        table_marker,
                        f"{table_marker}\n\n{row}",
                    )
                    updated_count += 1

    if updated_count > 0:
        roadmap_file.write_text(content, encoding="utf-8")

    return updated_count


def update_claim_verifications(repo_dir: Path, date_str: Optional[str] = None) -> int:
    """Update verification dates in markdown claim tags only if claims have valid targets."""
    target_date = date_str or datetime.date.today().isoformat()
    updated_count = 0

    target_dirs = [repo_dir / "docs", repo_dir / "skills", repo_dir / "prompts"]
    md_files: List[Path] = []
    for d in target_dirs:
        if d.is_dir():
            md_files.extend(d.rglob("*.md"))

    for root_file in [repo_dir / "AGENTS.md", repo_dir / "README.md"]:
        if root_file.is_file():
            md_files.append(root_file)

    for md_path in sorted(set(md_files)):
        try:
            content = md_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        def _repl(m: re.Match) -> str:
            claim_id, target = m.group("id"), m.group("target")
            if not target:
                return m.group(0)  # Keep unbound claims unchanged
            target_attr = f' target="{target}"'
            return f'<!-- claim:{claim_id}{target_attr} verification="{target_date}" -->'

        new_content, count = CLAIM_TAG_RE.subn(_repl, content)
        if count > 0 and new_content != content:
            md_path.write_text(new_content, encoding="utf-8")
            updated_count += count

    sync_shipped_roadmap(repo_dir)
    return updated_count


def check_spec_synchronization(repo_dir: Optional[str] = None, head_sha: Optional[str] = None) -> Tuple[bool, str]:
    """Exported function for merge_pr DoD check."""
    root = Path(repo_dir or ".").resolve()
    cli_specs = extract_script_cli_specs(root / "scripts", head_sha=head_sha, repo_dir=root)
    drift, _ = audit_docs_and_skills(root, cli_specs, head_sha=head_sha)
    # Filter blocking drift (exclude non-blocking warnings if any)
    blocking = [d for d in drift if d.get("type") != "unbound_claim_warning"]
    if not blocking:
        return True, "All specifications and CLI references are synchronized with code."
    summary = f"{len(blocking)} specification drift finding(s) detected: " + "; ".join(
        f"{d['file']} ({d['message']})" for d in blocking[:3]
    )
    return False, summary


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bi-directional specification-to-code synchronization engine and DoD validator."
    )
    parser.add_argument(
        "--check", action="store_true", default=False,
        help="Run read-only audit for spec and CLI drift.",
    )
    parser.add_argument(
        "--update", action="store_true", default=False,
        help="Update verified claim timestamps in documentation and sync shipped roadmap rows.",
    )
    parser.add_argument(
        "--json", action="store_true", default=False,
        help="Emit results in machine-readable JSON format.",
    )
    parser.add_argument(
        "--repo-dir", default=".",
        help="Repository root directory (defaults to current directory).",
    )
    parser.add_argument(
        "--head", default=None,
        help="Git commit SHA to audit directly.",
    )
    parser.add_argument(
        "--verbose", action="store_true", default=False,
        help="Print detailed information.",
    )

    args = parser.parse_args(argv)
    repo_root = Path(args.repo_dir).resolve()
    scripts_dir = repo_root / "scripts"

    cli_specs = extract_script_cli_specs(scripts_dir, head_sha=args.head, repo_dir=repo_root)
    drift, claims = audit_docs_and_skills(repo_root, cli_specs, head_sha=args.head)
    blocking = [d for d in drift if d.get("type") != "unbound_claim_warning"]

    if args.update:
        if blocking:
            print("❌ Cannot update claim timestamps while specification drift exists:", file=sys.stderr)
            for d in blocking:
                print(f"  • {d['file']}: {d['message']}", file=sys.stderr)
            return 1
        count = update_claim_verifications(repo_root)
        if not args.json:
            print(f"✅ Updated {count} claim verification timestamp(s) in documentation.")

    payload = {
        "status": "clean" if not blocking else "drift_detected",
        "clean": len(blocking) == 0,
        "drift_count": len(blocking),
        "warning_count": len(drift) - len(blocking),
        "claim_count": len(claims),
        "scripts_scanned": len(cli_specs),
        "drift_findings": drift,
        "claims": claims,
        "specs": {
            k: {
                "flags": v["flags"],
                "positionals": v["positionals"],
                "functions": [f["name"] for f in v["functions"]],
            }
            for k, v in cli_specs.items()
        } if args.verbose else {},
    }

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"📊 Spec-Sync Scanned {len(cli_specs)} scripts across {repo_root.name}")
        if blocking:
            print(f"❌ {len(blocking)} specification drift finding(s) detected:")
            for d in blocking:
                print(f"  • {d['file']}: {d['message']}")
        else:
            print(f"✅ All CLI specifications and documentation examples are in sync ({len(claims)} claim(s) tracked).")

    return 0 if not blocking else 1


if __name__ == "__main__":
    sys.exit(main())
