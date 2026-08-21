import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHILD_MARKER = "ARU_SUITE_HERMETIC_CHILD"


class SuiteHermeticityTests(unittest.TestCase):
    def test_supported_suite_does_not_create_operator_aru_state(self):
        if os.environ.get(CHILD_MARKER) == "1":
            return

        with tempfile.TemporaryDirectory() as raw:
            isolated_home = Path(raw) / "home"
            isolated_home.mkdir()
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(isolated_home),
                    "XDG_STATE_HOME": str(isolated_home / "state"),
                    "PYTHONDONTWRITEBYTECODE": "1",
                    CHILD_MARKER: "1",
                }
            )
            result = subprocess.run(
                [sys.executable, "-m", "unittest", "discover", "tests"],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
            )

            failures = []
            if result.returncode != 0:
                failures.append(
                    "child suite failed:\n"
                    + result.stdout[-4000:]
                    + "\n"
                    + result.stderr[-4000:]
                )
            aru_state = isolated_home / ".aru"
            if aru_state.exists():
                failures.append(
                    "child suite created persistent operator state: "
                    + ", ".join(
                        str(path.relative_to(isolated_home))
                        for path in sorted(aru_state.rglob("*"))
                    )
                )
            xdg_state = Path(environment["XDG_STATE_HOME"])
            xdg_entries = (
                sorted(xdg_state.rglob("*")) if xdg_state.exists() else []
            )
            if xdg_entries:
                failures.append(
                    "child suite created persistent XDG state: "
                    + ", ".join(
                        str(path.relative_to(xdg_state)) for path in xdg_entries
                    )
                )
            self.assertEqual(failures, [])

    def test_canonical_ci_verification_baseline_is_python_311(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        versions = re.findall(r"python-version:\s*['\"]?([^'\"\n]+)", workflow)
        self.assertTrue(versions)
        self.assertEqual({version.strip() for version in versions}, {"3.11"})


if __name__ == "__main__":
    unittest.main()
