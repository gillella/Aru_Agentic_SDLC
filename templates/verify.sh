#!/usr/bin/env bash
set -euo pipefail

echo "::error::.aru/verify.sh must contain the smallest affected verification commands appropriate to this repository and change (for example lint, type, build, or tests)."
echo "Configure it during bootstrap; keep broad main/release suites in separate workflows."
exit 1
