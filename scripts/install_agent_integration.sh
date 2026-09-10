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
governance_template="${aru_home}/templates/AGENTS.md"
managed_begin="<!-- BEGIN ARU_SDLC_GOVERNANCE -->"
managed_end="<!-- END ARU_SDLC_GOVERNANCE -->"
global_template="$(mktemp)"
trap 'rm -f "${global_template}"' EXIT
python3 - "${governance_template}" "${global_template}" <<'PYTHON'
import re
import sys
from pathlib import Path
text = Path(sys.argv[1]).read_text()
text, count = re.subn(
    r"This repository is scaffolded.*?whose declared profile and `runs-on:` disagree\.\s*",
    "Each repository must use its declared runner profile and trust boundary. "
    "Read its local AGENTS.md and .aru/verify.sh; global guidance does not "
    "select or change a repository's runner profile.\n",
    text, flags=re.S,
)
if count != 1 or "__ARU_" in text:
    raise SystemExit("error: global runner guidance could not be rendered")
Path(sys.argv[2]).write_text(text)
PYTHON

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
    local backup="${target}.pre-aru-v0.2.8.$(date +%Y%m%d%H%M%S)"
    cp -p "${target}" "${backup}"
    cp "${template}" "${target}"
    echo "migrated legacy Aru guidance in ${target}; preserved ${backup}"
    return
  fi

  temporary="$(mktemp)"
  awk 'FNR == 1 && NR != 1 { print "" } { print }' "${target}" "${template}" > "${temporary}"
  cat "${temporary}" > "${target}"
  rm -f "${temporary}"
  echo "appended managed Aru guidance to ${target}"
}

for target in "${HOME}/.codex/AGENTS.md" "${HOME}/.claude/CLAUDE.md"; do
  install_global_guidance "${target}" "${global_template}"
done

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

echo "installed ${#skills[@]} Aru minimal-kernel skills"
echo "set ARU_SDLC_HOME=${aru_home} in the environment used by your agents"
