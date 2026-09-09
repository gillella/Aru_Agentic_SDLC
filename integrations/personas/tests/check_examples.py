"""Run shipped offline examples; optionally check JSON Schema when installed."""
import json
from pathlib import Path
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "data/request.schema.json").read_text())
    try:
        import jsonschema
    except ImportError:
        jsonschema = None
    if jsonschema:
        jsonschema.Draft202012Validator.check_schema(schema)
    for path in sorted((root / "examples").glob("*.json")):
        if "binding" in path.name:
            continue
        raw = json.loads(path.read_text())
        if isinstance(raw["binding"], str):
            raw["binding"] = json.loads((path.parent / raw["binding"]).read_text())
        if jsonschema:
            jsonschema.validate(raw, schema)
        result = subprocess.run([sys.executable, "-m", "integrations.personas", "explain",
                                 "--input", str(path), "--at", "2026-09-09T18:00:00+00:00"],
                                capture_output=True, text=True, check=False)
        assert result.returncode == (2 if "refused" in path.name else 0), result.stdout + result.stderr
        output = json.loads(result.stdout)
        assert output["execution_authority"] is False
        print(f"{path.name}: exit={result.returncode}, selected={output['selected']}")
    print("5 synthetic examples passed; JSON Schema " + ("validated" if jsonschema else "not checked (optional jsonschema absent)"))


if __name__ == "__main__":
    main()
