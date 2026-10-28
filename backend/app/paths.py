"""Cross-platform path validation for model-controlled file names."""

from pathlib import Path, PurePosixPath, PureWindowsPath

from app.errors import HubError

EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".next",
}
SECRET_NAMES = {"id_rsa", "id_ed25519", "credentials", "credentials.json", ".npmrc", ".pypirc"}


def relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or value.startswith("/")
        or ":" in value
        or PureWindowsPath(value).drive
        or ".." in path.parts
        or any(ord(char) < 32 for char in value)
        or any(part.endswith((".", " ")) for part in path.parts)
        or str(path) != value
        or value == "."
    ):
        raise HubError("path_unsafe", "File paths must be normalized project-relative paths")
    for part in path.parts:
        if part.split(".")[0].upper() in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *(f"COM{n}" for n in range(1, 10)),
            *(f"LPT{n}" for n in range(1, 10)),
        }:
            raise HubError("path_unsafe", "Reserved Windows file name")
    return value


def is_secret(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    return (
        name == ".env"
        or name.startswith(".env.")
        or name in SECRET_NAMES
        or name.endswith((".pem", ".key", ".p12", ".pfx"))
    )


def allowed_source(path: str) -> bool:
    return not is_secret(path) and not any(
        part.lower() in EXCLUDED_DIRS for part in PurePosixPath(path).parts
    )


def resolve_inside(root: Path, value: str) -> Path:
    relative_path(value)
    target = root / value
    current = target
    while current != root:
        is_junction = getattr(current, "is_junction", lambda: False)
        if current.is_symlink() or is_junction():
            raise HubError("path_unsafe", "Symlinks and junctions are not permitted")
        current = current.parent
    if not target.resolve().is_relative_to(root.resolve()):
        raise HubError("path_unsafe", "Path escapes the project root")
    return target
