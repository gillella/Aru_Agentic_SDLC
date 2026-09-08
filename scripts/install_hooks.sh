#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source_dir="$(cd "${script_dir}/../hooks" && pwd)"
repo_root="$(git rev-parse --show-toplevel)"
git_common_dir="$(git -C "${repo_root}" rev-parse --path-format=absolute --git-common-dir)"
canonical_hooks_dir="${git_common_dir}/hooks"
hooks_dir="$(git -C "${repo_root}" rev-parse --path-format=absolute --git-path hooks)"
if [[ -L "${canonical_hooks_dir}" ]]; then
  echo "error: refusing symbolic-link hooks directory: ${canonical_hooks_dir}" >&2
  exit 1
fi
if [[ "${hooks_dir}" != "${canonical_hooks_dir}" ]]; then
  echo "error: refusing non-canonical core.hooksPath: ${hooks_dir}" >&2
  exit 1
fi
mkdir -p "${hooks_dir}"

is_managed_hook() {
  local ownership
  ownership="$(python3 - "$1" <<'PY'
import hashlib
import pathlib
import sys

content = pathlib.Path(sys.argv[1]).read_bytes()
marker = b"# Aru managed pre-push hook; installed by scripts/install_hooks.sh"
# Exact historical hooks, including versions that predate the stable marker.
historical = {
    "e48349eecc28843666dfcc6bd4b00fc2a3d1554b8d7445a73219fa58564bbc86",  # 7a3dc46
    "9155c01bc3f07c2fb63dc32da88a0acce723c71e360f1d3893fe2cb73a80073f",  # 2b55cfc
    "c5fb32ec1308c4d96d75af5d6b21cb316d90e0de1c4d4752e4fa32a2ae9cdc82",  # d3a0588
    "37e230405209654efcc5a9e52b6a76fac7f3635e16dd675d01f4ea1ec3bb0e69",  # 4393d3c
    "8a48edb4382f41011e9f1227b07f7a7ffe9980030be2a1f462d5ce3f5a20b195",  # 2dfe4ce
    "289bf4c31078ca5aa0fc994a56a0f914f610e92c598b79aeebefbff71ae35d38",  # a1559da
}
managed = content.splitlines()[1:2] == [marker] or hashlib.sha256(content).hexdigest() in historical
# Custom references to the reserved backup may become self-calls after relocation.
# Refuse ambiguity; a text match must never authorize deleting a custom hook.
if not managed and b"pre-push.pre-aru" in content:
    sys.exit("error: custom hook references reserved backup path; operator review required")
print("managed" if managed else "custom")
PY
)" || { echo "error: cannot safely identify hook ownership: $1" >&2; exit 1; }
  [[ "${ownership}" == managed ]]
}

for hook_name in pre-push pre-push.pre-aru enforce_touches.py touches.py; do
  hook_target="${hooks_dir}/${hook_name}"
  if [[ -L "${hook_target}" ]]; then
    echo "error: refusing symbolic-link hook target: ${hook_target}" >&2
    exit 1
  fi
  if [[ -e "${hook_target}" && ! -f "${hook_target}" ]]; then
    echo "error: refusing non-file hook target: ${hook_target}" >&2
    exit 1
  fi
done

# Classify both paths before modifying either; two custom hooks are ambiguous.
current_custom=false
prior_custom=false
if [[ -f "${hooks_dir}/pre-push" ]] && ! is_managed_hook "${hooks_dir}/pre-push"; then
  current_custom=true
fi
if [[ -f "${hooks_dir}/pre-push.pre-aru" ]] &&
   ! is_managed_hook "${hooks_dir}/pre-push.pre-aru"; then
  prior_custom=true
fi
if ${current_custom} && ${prior_custom}; then
  echo "error: existing pre-push and pre-push.pre-aru both need operator review" >&2
  exit 1
fi
if ! ${prior_custom}; then
  rm -f "${hooks_dir}/pre-push.pre-aru"
fi
if ${current_custom}; then
  mv "${hooks_dir}/pre-push" "${hooks_dir}/pre-push.pre-aru"
fi

cp "${source_dir}/pre-push" "${hooks_dir}/pre-push"
cp "${source_dir}/enforce_touches.py" "${hooks_dir}/enforce_touches.py"
cp "${script_dir}/touches.py" "${hooks_dir}/touches.py"
chmod +x "${hooks_dir}/pre-push" "${hooks_dir}/enforce_touches.py"
echo "installed Aru hooks in ${hooks_dir}"
