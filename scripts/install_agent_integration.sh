#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
aru_home="$(cd "${script_dir}/.." && pwd)"
skills=(
  address-pr-feedback
  create-github-issue
  implement-next-issue
  init-agent-project
  remediate-ci-failure
  triage-backlog
)
targets=(
  "${HOME}/.agents/skills"
  "${HOME}/.codex/skills"
  "${HOME}/.cursor/skills"
  "${HOME}/.claude/skills"
)
# Hermes Agent has no plugin mechanism, so a local skills directory and a managed
# block in its own instruction file are the only way it can carry these skills.
# It discovers <category>/<skill>/SKILL.md, so the category directory is the
# target and the six skills become its children. Both Hermes paths are added
# only when ~/.hermes already exists: this installer configures agents that are
# present and never creates a home for one that is not.
hermes_home="${HOME}/.hermes"
hermes_skills="${hermes_home}/skills/software-development"
hermes_guidance="${hermes_home}/SOUL.md"
if [[ -d "${hermes_home}" && ! -L "${hermes_home}" ]]; then
  targets+=("${hermes_skills}")
fi
governance_template="${aru_home}/templates/AGENTS.md"
managed_begin="<!-- BEGIN ARU_SDLC_GOVERNANCE -->"
managed_end="<!-- END ARU_SDLC_GOVERNANCE -->"
project=""
if [[ $# -gt 0 ]]; then
  if [[ $# -ne 2 || "$1" != "--project" || -z "$2" ]]; then
    echo "usage: $0 [--project path]" >&2
    exit 2
  fi
  project="$2"
fi
global_template="$(mktemp)"
trap 'rm -f "${global_template}"' EXIT
PYTHONPATH="${aru_home}/scripts" python3 -c 'import policy, sys; sys.stdout.write(policy.genericized_agent_guidance())' > "${global_template}" \
  || { echo "error: global runner guidance could not be rendered" >&2; exit 1; }

retained() {
  local candidate="$1"
  local name
  for name in "${skills[@]}"; do
    [[ "${candidate}" == "${name}" ]] && return 0
  done
  return 1
}

install_global_guidance() {
  local target="$1"
  local template="$2"
  local target_dir
  local temporary

  target_dir="$(dirname "${target}")"
  if [[ -L "${target_dir}" || -L "${target}" ]]; then
    echo "error: refusing symlinked global guidance path ${target}" >&2
    exit 1
  fi
  mkdir -p "${target_dir}"

  if [[ ! -e "${target}" ]]; then
    cp "${template}" "${target}"
    echo "installed managed Aru guidance in ${target}"
    return
  fi

  # Validate exact boundaries before either replacement path can discard text.
  if grep -Fq "${managed_begin}" "${target}" || grep -Fq "${managed_end}" "${target}"; then
    python3 - "${target}" <<'PYTHON'
import sys
from pathlib import Path
text = Path(sys.argv[1]).read_text()
markers = ("<!-- BEGIN ARU_SDLC_GOVERNANCE -->", "<!-- END ARU_SDLC_GOVERNANCE -->")
if any(text.count(m) != 1 or m not in text.splitlines() for m in markers) or text.index(markers[0]) > text.index(markers[1]):
    raise SystemExit("error: refusing ambiguous Aru managed boundaries")
PYTHON
  fi

  # The known Claude directive predates the managed block. Replace that Aru
  # section through its closing marker, preserving personal text on both sides.
  if grep -Fxq "# MASTER OPERATING DIRECTIVE: Aru_Agentic_SDLC" "${target}"; then
    if [[ "$(grep -Fc "${managed_begin}" "${target}")" != 1 ||
          "$(grep -Fc "${managed_end}" "${target}")" != 1 ]]; then
      echo "error: refusing ambiguous legacy Aru guidance in ${target}" >&2
      exit 1
    fi
    python3 - "${target}" "${template}" <<'PYTHON'
import sys
from pathlib import Path
from datetime import datetime, timezone
p, template = map(Path, sys.argv[1:])
text = p.read_text()
start = text.index("# MASTER OPERATING DIRECTIVE: Aru_Agentic_SDLC")
begin = text.index("<!-- BEGIN ARU_SDLC_GOVERNANCE -->")
end = text.index("<!-- END ARU_SDLC_GOVERNANCE -->")
if not start < begin < end or text.count("# MASTER OPERATING DIRECTIVE: Aru_Agentic_SDLC") != 1:
    raise SystemExit("error: refusing ambiguous legacy Aru guidance")
if text[start:begin].strip() != "# MASTER OPERATING DIRECTIVE: Aru_Agentic_SDLC\nrun-aru-factory":
    raise SystemExit("error: refusing unknown legacy Aru guidance")
end += len("<!-- END ARU_SDLC_GOVERNANCE -->")
backup = p.with_name(p.name + ".pre-aru-v2." + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f"))
backup.write_bytes(p.read_bytes())
p.write_text(text[:start] + template.read_text().rstrip("\n") + text[end:])
print(f"migrated legacy Aru guidance in {p}; preserved {backup}")
PYTHON
    return
  fi

  if grep -Fq "${managed_begin}" "${target}"; then
    if ! grep -Fq "${managed_end}" "${target}"; then
      echo "error: refusing to edit malformed Aru block in ${target}" >&2
      exit 1
    fi
    python3 - "${target}" <<'PYTHON'
import sys
from pathlib import Path
text = Path(sys.argv[1]).read_text()
begin, end = "<!-- BEGIN ARU_SDLC_GOVERNANCE -->", "<!-- END ARU_SDLC_GOVERNANCE -->"
outside = text[:text.index(begin)] + text[text.index(end) + len(end):]
if any(word in outside for word in ("run-aru-factory", "code-review", "fetch_next_issue.py")):
    raise SystemExit("error: refusing stale Aru guidance outside managed block")
PYTHON
    temporary="$(mktemp)"
    awk -v begin="${managed_begin}" -v end="${managed_end}" -v template="${template}" '
      index($0, begin) {
        while ((getline line < template) > 0) print line
        close(template)
        inside = 1
        next
      }
      index($0, end) { inside = 0; next }
      !inside { print }
    ' "${target}" > "${temporary}"
    cat "${temporary}" > "${target}"
    rm -f "${temporary}"
    echo "updated managed Aru guidance in ${target}"
    return
  fi

  if grep -Fq "# Global Software Development Governance: Aru_Agentic_SDLC" "${target}"; then
    if [[ "$(cat "${target}")" != $'# Global Software Development Governance: Aru_Agentic_SDLC\nrun-aru-factory' ]]; then
      echo "error: refusing unknown legacy Aru guidance in ${target}" >&2
      exit 1
    fi
    local backup="${target}.pre-aru-v0.2.8.$(date +%Y%m%d%H%M%S)"
    cp -p "${target}" "${backup}"
    cp "${template}" "${target}"
    echo "migrated legacy Aru guidance in ${target}; preserved ${backup}"
    return
  fi

  if grep -Eq 'run-aru-factory|code-review|fetch_next_issue\.py|MASTER OPERATING DIRECTIVE' "${target}"; then
    echo "error: refusing unknown stale guidance in ${target}" >&2
    exit 1
  fi

  temporary="$(mktemp)"
  awk 'FNR == 1 && NR != 1 { print "" } { print }' "${target}" "${template}" > "${temporary}"
  cat "${temporary}" > "${target}"
  rm -f "${temporary}"
  echo "appended managed Aru guidance to ${target}"
}

remove_claude_managed_guidance() {
  local target="${HOME}/.claude/CLAUDE.md"
  [[ -e "${target}" || -L "${target}" ]] || return 0
  if [[ -L "${HOME}/.claude" || -L "${target}" || ! -f "${target}" ]]; then
    echo "error: refusing unsafe Claude guidance ${target}" >&2
    exit 1
  fi
  python3 - "${target}" <<'PYTHON'
import sys
from datetime import datetime, timezone
from pathlib import Path
p = Path(sys.argv[1])
text = p.read_text(encoding="utf-8")
begin, end = "<!-- BEGIN ARU_SDLC_GOVERNANCE -->", "<!-- END ARU_SDLC_GOVERNANCE -->"
title = "# MASTER OPERATING DIRECTIVE: Aru_Agentic_SDLC"
if begin not in text and end not in text:
    if title in text or "# Global Software Development Governance: Aru_Agentic_SDLC" in text:
        raise SystemExit("error: refusing unknown Claude legacy guidance with plugin")
    if any(word in text for word in ("run-aru-factory", "code-review", "fetch_next_issue.py")):
        raise SystemExit("error: refusing unknown stale Claude guidance with plugin")
else:
    if any(text.count(marker) != 1 or marker not in text.splitlines() for marker in (begin, end)):
        raise SystemExit("error: refusing ambiguous Aru managed boundaries")
    start, finish = text.index(begin), text.index(end) + len(end)
    if start >= finish:
        raise SystemExit("error: refusing ambiguous Aru managed boundaries")
    if title in text:
        if text.count(title) != 1:
            raise SystemExit("error: refusing unknown Claude legacy guidance with plugin")
        start = text.index(title)
        if (start >= text.index(begin)
                or text[start:text.index(begin)].strip() != title + "\nrun-aru-factory"):
            raise SystemExit("error: refusing ambiguous Claude legacy guidance")
    outside = text[:start] + text[finish:]
    if any(word in outside for word in ("run-aru-factory", "code-review", "fetch_next_issue.py")):
        raise SystemExit("error: refusing stale Claude guidance outside managed block")
    backup = p.with_name(p.name + ".pre-aru-v2." + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f"))
    backup.write_bytes(p.read_bytes())
    p.write_text(outside, encoding="utf-8")
    print(f"removed duplicate Claude managed guidance in {p}; preserved {backup}")
PYTHON
}

install_cursor_project_rule() {
  local rule="${project}/.cursor/rules/aru-agentic-sdlc.mdc"
  if [[ ! -d "${project}" || ! -f "${rule}" || -L "${project}" || -L "${project}/.cursor" || -L "${project}/.cursor/rules" || -L "${rule}" ]]; then
    echo "error: refusing unsafe Cursor project rule ${rule}" >&2
    exit 1
  fi
  python3 - "${rule}" "${global_template}" <<'PYTHON'
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

rule, template = map(Path, sys.argv[1:])
text = rule.read_text(encoding="utf-8")
begin, end = "<!-- BEGIN ARU_SDLC_GOVERNANCE -->", "<!-- END ARU_SDLC_GOVERNANCE -->"
header = ("---\ndescription: Aru_Agentic_SDLC Issue-First governance for this repository\n"
          "alwaysApply: true\n---\n\n# Aru Agentic SDLC (project rule)\n\n"
          "This repository is governed by **Aru_Agentic_SDLC**.\n\n")
if not text.startswith(header):
    raise SystemExit("error: refusing unknown Cursor project rule")
if begin in text or end in text:
    if any(text.count(marker) != 1 or marker not in text.splitlines() for marker in (begin, end)):
        raise SystemExit("error: refusing ambiguous Aru managed boundaries")
    start, finish = text.index(begin), text.index(end) + len(end)
    if start >= finish or start < len(header):
        raise SystemExit("error: refusing ambiguous Aru managed boundaries")
else:
    section = "## Before any code change\n\n"
    boundary = "\n## Hard constraints\n"
    if text.count(section) != 1 or text.count(boundary) != 1:
        raise SystemExit("error: refusing unknown legacy Cursor rule")
    start = text.index(section)
    finish = text.index(boundary)
    body = text[start:finish].strip().splitlines()
    routes = [line for line in body if line.startswith("   - ")]
    expected = {"run-aru-factory", "implement-next-issue", "create-github-issue",
                "code-review", "remediate-ci-failure", "address-pr-feedback",
                "init-agent-project"}
    known_lines = {
        '   - `run-aru-factory` — `aru code` (synonyms software/dev/sdlc), "please continue", work the board, loop',
        '   - `implement-next-issue` — claim/implement/PR for a named or next issue',
        '   - `create-github-issue` — file work',
        '   - `code-review` — review only a preassigned `review:agent` emergency fallback; otherwise refuse and await external review',
        '   - `code-review` — review a PR',
        '   - `remediate-ci-failure` — fix red CI',
        '   - `address-pr-feedback` — resolve review threads',
        '   - `init-agent-project` — bootstrap a new governed repo',
    }
    names = {re.match(r"   - `([^`]+)`", line).group(1) for line in routes
             if re.match(r"   - `([^`]+)`", line)}
    if (not text.startswith(header, 0, start) or names != expected or len(routes) != len(expected)
            or any(line not in known_lines for line in routes)
            or body[:3] != ["## Before any code change", "",
                             "1. Confirm work originates from a tracked GitHub issue (Issue-First Law)."]
            or body[3] != "2. Read and follow the matching skill under `$ARU_SDLC_HOME/skills/`:"
            or body[-1] != '3. Prefer `python3 "$ARU_SDLC_HOME/scripts/<tool>.py"` over ad-hoc GitHub/git glue.'
            or len(body) != 5 + len(routes)):
        raise SystemExit("error: refusing unknown legacy Cursor rule")
    if any(re.search(r"run-aru-factory|code-review|fetch_next_issue\.py|review:agent", line)
           for line in text[finish:].splitlines()):
        raise SystemExit("error: refusing stale Cursor rule outside known section")

if begin in text:
    outside = text[:start] + text[finish:]
    if any(word in outside for word in ("run-aru-factory", "code-review", "fetch_next_issue.py")):
        raise SystemExit("error: refusing stale Cursor rule outside managed block")

replacement = template.read_text(encoding="utf-8").rstrip("\n")
updated = text[:start] + replacement + text[finish:]
if updated != text:
    backup = rule.with_name(rule.name + ".pre-aru-v2." + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f"))
    backup.write_bytes(rule.read_bytes())
    rule.write_text(updated, encoding="utf-8")
    print(f"updated managed Cursor project rule in {rule}; preserved {backup}")
PYTHON
}

plugin_installed=0
installed="${HOME}/.claude/plugins/installed_plugins.json"
if [ -f "${installed}" ] && python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if any(k.startswith("aru-codefactory@") for k in d.get("plugins", {})) else 1)' "${installed}"; then
  plugin_installed=1
fi

guidance_targets=("${HOME}/.codex/AGENTS.md" "${HOME}/.claude/CLAUDE.md")
if [[ -d "${hermes_home}" && ! -L "${hermes_home}" ]]; then
  guidance_targets+=("${hermes_guidance}")
fi
for target in "${guidance_targets[@]}"; do
  if [[ "${target}" == "${HOME}/.claude/CLAUDE.md" && "${plugin_installed}" -eq 1 ]]; then
    remove_claude_managed_guidance
    continue
  fi
  install_global_guidance "${target}" "${global_template}"
done
if [[ -n "${project}" ]]; then
  install_cursor_project_rule
fi

for target in "${targets[@]}"; do
  mkdir -p "${target}"
  for existing in "${target}"/*; do
    [[ -L "${existing}" ]] || continue
    destination="$(readlink "${existing}")"
    if [[ "${destination}" == "${aru_home}/skills/"* ]]; then
      name="$(basename "${existing}")"
      retained "${name}" || rm -f "${existing}"
    fi
  done
  if [[ "${target}" == "${HOME}/.claude/skills" && "${plugin_installed}" -eq 1 ]]; then continue; fi
  for name in "${skills[@]}"; do
    source="${aru_home}/skills/${name}"
    destination="${target}/${name}"
    if [[ -e "${destination}" && ! -L "${destination}" ]]; then
      echo "error: refusing to overwrite user-owned ${destination}" >&2
      exit 1
    fi
    rm -f "${destination}"
    ln -s "${source}" "${destination}"
  done
done

if [[ "${plugin_installed}" -eq 1 ]]; then
  echo "skipped ~/.claude/skills and ~/.claude/CLAUDE.md: the aru-codefactory plugin supplies the six skills and the governance block"
fi
if [[ ! -d "${hermes_home}" || -L "${hermes_home}" ]]; then
  echo "skipped Hermes Agent: ${hermes_home} does not exist"
fi
echo "installed ${#skills[@]} Aru minimal-kernel skills"
echo "set ARU_SDLC_HOME=${aru_home} in the environment used by your agents"
