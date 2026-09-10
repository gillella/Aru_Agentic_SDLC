#!/usr/bin/env bash
# ARU_CONSUMER_VERIFICATION_UNCONFIGURED
# Replace this starter with this project's build, tests, or other required
# checks, and remove the marker above. Preserve existing consumer checks when
# upgrading Aru. This script runs from the repository root and must not deploy
# or change external systems. A failed command must make verification fail.
set -euo pipefail

echo "::error::Consumer verification is not configured. Add the project's required checks to .aru/verify-project.sh." >&2
exit 1
