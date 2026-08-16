#!/usr/bin/env python3
"""Build a contained, public-only static preview artifact."""

import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import Optional, Tuple


PUBLIC_EXTENSIONS = {
    ".avif", ".css", ".gif", ".htm", ".html", ".ico", ".jpeg", ".jpg",
    ".js", ".mjs", ".otf", ".png", ".svg", ".ttf", ".txt", ".webmanifest",
    ".webp", ".woff", ".woff2", ".xml",
}
PUBLIC_JSON_NAMES = {"manifest.json"}
IGNORED_NAMES = {"node_modules", "__pycache__"}
SECRET_NAME_PARTS = {
    "credential", "credentials", "password", "private", "secret", "token",
}


def _absolute_lexical(path: str) -> Path:
    candidate = Path(path).expanduser()
    return candidate if candidate.is_absolute() else Path.cwd() / candidate


def _contains_symlink(path: Path) -> bool:
    if path.is_symlink():
        return True
    if not path.exists() or not path.is_dir():
        return False
    for current, directories, files in os.walk(path, followlinks=False):
        current_path = Path(current)
        for name in directories + files:
            if (current_path / name).is_symlink():
                return True
    return False


def _has_symlink_component(path: Path, stop: Path) -> bool:
    current = path
    while current != stop:
        if current.exists() and current.is_symlink():
            return True
        if current.parent == current:
            return True
        current = current.parent
    return stop.is_symlink()


def _is_public_file(path: Path) -> bool:
    lowered = path.name.lower()
    if path.name.startswith("."):
        return False
    if any(part in lowered for part in SECRET_NAME_PARTS):
        return False
    if path.suffix.lower() == ".json":
        return lowered in PUBLIC_JSON_NAMES
    return path.suffix.lower() in PUBLIC_EXTENSIONS


def _validate_public_tree(root: Path) -> bool:
    if _contains_symlink(root):
        return False
    for current, directories, files in os.walk(root, followlinks=False):
        if any(name.startswith(".") or name in IGNORED_NAMES for name in directories):
            return False
        current_path = Path(current)
        for name in files:
            if not _is_public_file(current_path / name):
                return False
    return (root / "index.html").is_file() and not (root / "index.html").is_symlink()


def _copy_public_tree(source: Path, destination: Path) -> None:
    for current, directories, files in os.walk(source, followlinks=False):
        directories[:] = [
            name for name in directories
            if not name.startswith(".") and name not in IGNORED_NAMES
        ]
        current_path = Path(current)
        relative = current_path.relative_to(source)
        target_dir = destination / relative
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in files:
            source_file = current_path / name
            if _is_public_file(source_file):
                shutil.copy2(source_file, target_dir / name)


APP_CANDIDATE_DIRS = ["sdlc_flow_visualizer", "public", "dist", "build", "web", "frontend", "site"]


def find_preview_source(root_dir: str) -> Optional[Tuple[str, str]]:
    """Return a non-symlink static source rooted inside ``root_dir``."""
    lexical_root = _absolute_lexical(root_dir)
    if lexical_root.is_symlink() or not lexical_root.is_dir():
        return None
    root = lexical_root.resolve(strict=True)

    for candidate in APP_CANDIDATE_DIRS:
        candidate_path = root / candidate
        index = candidate_path / "index.html"
        if (
            candidate_path.is_dir()
            and not candidate_path.is_symlink()
            and index.is_file()
            and not index.is_symlink()
        ):
            return "app_dir", str(candidate_path)

    root_index = root / "index.html"
    if root_index.is_file() and not root_index.is_symlink():
        return "root_static", str(root)

    docs = root / "docs"
    docs_index = docs / "index.html"
    if docs.is_dir() and not docs.is_symlink() and docs_index.is_file() and not docs_index.is_symlink():
        return "app_dir", str(docs)
    return None


def detect_surface_classification(root_dir: str) -> Tuple[str, Optional[Tuple[str, str]]]:
    """Distinguish valid runnable products, broken runnable products (missing entrypoint), and libraries."""
    lexical_root = _absolute_lexical(root_dir)
    if lexical_root.is_symlink() or not lexical_root.is_dir():
        return "error", None
    root = lexical_root.resolve(strict=True)

    found = find_preview_source(str(root))
    if found:
        return "runnable_found", found

    has_library_marker = (
        (root / "src").is_dir()
        or (root / "pyproject.toml").is_file()
        or (root / "setup.py").is_file()
        or (root / "setup.cfg").is_file()
        or (root / "requirements.txt").is_file()
    )

    explicit_app_dirs = ["sdlc_flow_visualizer", "public", "web", "frontend", "site"]
    for candidate in explicit_app_dirs:
        candidate_path = root / candidate
        if candidate_path.is_dir() and not candidate_path.is_symlink():
            return "runnable_missing_entrypoint", ("app_dir", str(candidate_path))

    if has_library_marker:
        return "library", None

    for candidate in ("dist", "build"):
        candidate_path = root / candidate
        if candidate_path.is_dir() and not candidate_path.is_symlink():
            return "runnable_missing_entrypoint", ("app_dir", str(candidate_path))

    return "unknown", None


def _set_github_output(name: str, value: str) -> None:
    """Export step output to GitHub Actions if GITHUB_OUTPUT is defined."""
    output_file = os.environ.get("GITHUB_OUTPUT")
    if not output_file:
        return
    try:
        with open(output_file, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")
    except OSError:
        pass


def assemble_preview_artifact(source_dir: str, output_dir: str, allow_library: bool = False) -> bool:
    """Copy only public static files into the source checkout's canonical dist/."""
    lexical_source = _absolute_lexical(source_dir)
    if lexical_source.is_symlink() or not lexical_source.is_dir():
        print("[ERROR] Preview source must be a real directory, not a symlink.", file=sys.stderr)
        _set_github_output("has_preview", "false")
        _set_github_output("is_library", "false")
        return False
    source_root = lexical_source.resolve(strict=True)

    lexical_dest = _absolute_lexical(output_dir)
    if lexical_dest.is_symlink():
        print("[ERROR] Preview output must not be a symlink.", file=sys.stderr)
        _set_github_output("has_preview", "false")
        _set_github_output("is_library", "false")
        return False
    dest = lexical_dest.resolve(strict=False)
    try:
        relative_dest = dest.relative_to(source_root)
    except ValueError:
        print("[ERROR] Preview output must be contained inside the source checkout.", file=sys.stderr)
        _set_github_output("has_preview", "false")
        _set_github_output("is_library", "false")
        return False
    if relative_dest == Path(".") or _has_symlink_component(dest, source_root):
        print("[ERROR] Preview output must be a non-symlink descendant of the source checkout.", file=sys.stderr)
        _set_github_output("has_preview", "false")
        _set_github_output("is_library", "false")
        return False
    if dest != source_root / "dist":
        print("[ERROR] Preview output must be the canonical <source>/dist directory.", file=sys.stderr)
        _set_github_output("has_preview", "false")
        _set_github_output("is_library", "false")
        return False

    classification, found = detect_surface_classification(str(source_root))
    if classification == "runnable_missing_entrypoint":
        app_path = found[1] if found else "app"
        print(
            f"[ERROR] Runnable application directory '{app_path}' exists but lacks entrypoint index.html.",
            file=sys.stderr,
        )
        _set_github_output("has_preview", "false")
        _set_github_output("is_library", "false")
        return False

    if classification == "library":
        if allow_library:
            print(
                f"[INFO] Product in '{source_root}' identified as a library with no runnable web surface; "
                "skipping preview build visibly.",
            )
            _set_github_output("has_preview", "false")
            _set_github_output("is_library", "true")
            return True
        print(
            f"[ERROR] Product in '{source_root}' is a library with no deployable preview surface and --allow-library was not set.",
            file=sys.stderr,
        )
        _set_github_output("has_preview", "false")
        _set_github_output("is_library", "true")
        return False

    if classification != "runnable_found" or not found:
        print(
            f"[ERROR] No deployable static entrypoint (index.html) found in '{source_root}' "
            "or a supported public subdirectory.",
            file=sys.stderr,
        )
        _set_github_output("has_preview", "false")
        _set_github_output("is_library", "false")
        return False

    source_type, source_path = found
    src = Path(source_path)
    if _contains_symlink(src):
        print("[ERROR] Preview source contains a symlink and cannot be published safely.", file=sys.stderr)
        _set_github_output("has_preview", "false")
        _set_github_output("is_library", "false")
        return False

    if src == dest:
        if _validate_public_tree(dest):
            print(f"Preview artifact already present in '{dest.name}/'")
            _set_github_output("has_preview", "true")
            _set_github_output("is_library", "false")
            return True
        print("[ERROR] Existing preview artifact contains non-public or unsafe files.", file=sys.stderr)
        _set_github_output("has_preview", "false")
        _set_github_output("is_library", "false")
        return False

    if source_type == "app_dir":
        try:
            src.relative_to(dest)
        except ValueError:
            pass
        else:
            print("[ERROR] Preview output cannot contain its own source directory.", file=sys.stderr)
            _set_github_output("has_preview", "false")
            _set_github_output("is_library", "false")
            return False
        try:
            dest.relative_to(src)
        except ValueError:
            pass
        else:
            print("[ERROR] Preview output cannot be inside its source directory.", file=sys.stderr)
            _set_github_output("has_preview", "false")
            _set_github_output("is_library", "false")
            return False

    public_directories = []
    if source_type == "root_static":
        for directory_name in ["docs", "static", "assets", "css", "js", "styles", "img", "images", "media"]:
            directory = source_root / directory_name
            if not directory.is_dir() or directory == dest:
                continue
            try:
                dest.relative_to(directory)
            except ValueError:
                pass
            else:
                print(
                    f"[ERROR] Preview output cannot be inside public asset directory '{directory_name}'.",
                    file=sys.stderr,
                )
                _set_github_output("has_preview", "false")
                _set_github_output("is_library", "false")
                return False
            public_directories.append((directory_name, directory))

    if dest.exists():
        if _contains_symlink(dest):
            print("[ERROR] Existing preview output contains a symlink.", file=sys.stderr)
            _set_github_output("has_preview", "false")
            _set_github_output("is_library", "false")
            return False
        if not dest.is_dir():
            print("[ERROR] Preview output exists and is not a directory.", file=sys.stderr)
            _set_github_output("has_preview", "false")
            _set_github_output("is_library", "false")
            return False
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=False)

    if source_type == "app_dir":
        _copy_public_tree(src, dest)
    else:
        root_files = [item for item in source_root.iterdir() if item.is_file() and _is_public_file(item)]
        for item in root_files:
            shutil.copy2(item, dest / item.name)
        for directory_name, directory in public_directories:
            if _contains_symlink(directory):
                print(f"[ERROR] Public asset directory '{directory_name}' contains a symlink.", file=sys.stderr)
                shutil.rmtree(dest)
                _set_github_output("has_preview", "false")
                _set_github_output("is_library", "false")
                return False
            _copy_public_tree(directory, dest / directory_name)

    if not (dest / "index.html").is_file():
        shutil.rmtree(dest)
        print("[ERROR] Preview artifact does not contain index.html.", file=sys.stderr)
        _set_github_output("has_preview", "false")
        _set_github_output("is_library", "false")
        return False
    print(f"Assembled public preview artifact into '{dest.name}/'")
    _set_github_output("has_preview", "true")
    _set_github_output("is_library", "false")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Assemble a contained static preview artifact.")
    parser.add_argument("--source", default=".", help="Project source directory (default: .)")
    parser.add_argument(
        "--output",
        help="Canonical <source>/dist output directory (default: <source>/dist)",
    )
    parser.add_argument(
        "--allow-library",
        action="store_true",
        help="Permit checkouts with no runnable preview surface (library mode)",
    )
    args = parser.parse_args()
    output = args.output or str(Path(args.source) / "dist")
    return 0 if assemble_preview_artifact(args.source, output, allow_library=args.allow_library) else 1


if __name__ == "__main__":
    sys.exit(main())
