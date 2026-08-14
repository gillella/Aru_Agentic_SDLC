#!/usr/bin/env python3
"""Build and bundle static web application preview artifacts.

Finds and copies the primary web application entrypoint and its associated assets
into the target distribution directory. Fails if no deployable static artifact or
index.html entrypoint is found.
"""

import argparse
import shutil
import sys
from pathlib import Path
from typing import Optional, Tuple


def find_preview_source(root_dir: str) -> Optional[Tuple[str, str]]:
    """Identifies the deployable preview source directory or entrypoint.

    Returns:
        Tuple of (source_type, source_path) or None if unsupported.
        - ("app_dir", path): A dedicated web application directory with index.html (e.g. sdlc_flow_visualizer, public, dist)
        - ("root_static", path): Root directory containing index.html and static assets
    """
    root = Path(root_dir).resolve()

    # 1. Candidate web application directories (first priority)
    candidate_dirs = ["sdlc_flow_visualizer", "public", "dist", "build", "web", "frontend", "site"]
    for candidate in candidate_dirs:
        cand_path = root / candidate
        if cand_path.is_dir() and (cand_path / "index.html").is_file():
            return "app_dir", str(cand_path)

    # 2. Root index.html
    if (root / "index.html").is_file():
        return "root_static", str(root)

    # 3. Docs index.html fallback if docs contains an index.html
    if (root / "docs" / "index.html").is_file():
        return "app_dir", str(root / "docs")

    return None


def assemble_preview_artifact(source_dir: str, output_dir: str) -> bool:
    """Assembles web assets from source_dir into output_dir.

    Returns True if successfully assembled with a valid index.html, False otherwise.
    """
    source_root = Path(source_dir).resolve()
    dest = Path(output_dir).resolve()

    found = find_preview_source(str(source_root))
    if not found:
        print(
            f"[ERROR] No deployable static entrypoint (index.html) found in '{source_root}' "
            f"or supported subdirectories (sdlc_flow_visualizer, public, dist, docs).",
            file=sys.stderr,
        )
        return False

    source_type, source_path = found

    # Clean / prepare output directory
    if dest.exists() and dest != source_root:
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)

    if source_type == "app_dir":
        src = Path(source_path)
        if src == dest:
            return True
        for item in src.iterdir():
            if item.name.startswith(".") or item.name in {"node_modules", "__pycache__"}:
                continue
            target = dest / item.name
            if item.is_dir():
                shutil.copytree(item, target)
            else:
                shutil.copy2(item, target)
        print(f"✅ Assembled preview artifact from '{src.name}/' into '{dest.name}/'")
        return True

    elif source_type == "root_static":
        shutil.copy2(source_root / "index.html", dest / "index.html")

        static_dir_names = ["docs", "static", "assets", "css", "js", "styles", "img", "images", "media"]
        for dname in static_dir_names:
            dir_path = source_root / dname
            if dir_path.is_dir() and dir_path != dest:
                shutil.copytree(dir_path, dest / dname)

        asset_exts = {".css", ".js", ".png", ".svg", ".ico", ".jpg", ".jpeg", ".webp", ".json"}
        for item in source_root.iterdir():
            if item.is_file() and item.suffix.lower() in asset_exts and item.name != "package.json":
                shutil.copy2(item, dest / item.name)

        print(f"✅ Assembled preview artifact from root static files into '{dest.name}/'")
        return True

    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Assemble deployable static preview artifact.")
    parser.add_argument("--source", default=".", help="Project source directory (default: .)")
    parser.add_argument("--output", default="dist", help="Output directory for deployment artifact (default: dist)")
    args = parser.parse_args()

    success = assemble_preview_artifact(args.source, args.output)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
