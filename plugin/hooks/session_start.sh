#!/usr/bin/env bash
# Advisory plugin hook. Real enforcement lives in hooks/pre-push, hooks/enforce_touches.py,
# aru-governed-pr and aru-merge-policy checks, and merge_pr.py; this hook adds none.
# 1. Exports ARU_SDLC_HOME for the session when unset, pointing at the installed plugin,
#    so every skill's `python3 "$ARU_SDLC_HOME/scripts/<cmd>.py"` resolves.
# 2. Emits the genericized governance guidance as additional context.
set -u
root="${CLAUDE_PLUGIN_ROOT:-}"
[ -n "${root}" ] || exit 0
if [ -n "${CLAUDE_ENV_FILE:-}" ] && [ -z "${ARU_SDLC_HOME:-}" ]; then
  printf 'export ARU_SDLC_HOME=%q
' "${root}" >> "${CLAUDE_ENV_FILE}"
fi
context="$(PYTHONPATH="${root}/scripts" python3 -c 'import policy; print(policy.genericized_agent_guidance())' 2>/dev/null)" || exit 0
python3 - "${context}" <<'PY'
import json, sys
print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                         "additionalContext": sys.argv[1]}}))
PY
exit 0
